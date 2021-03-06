from __future__ import absolute_import, division, print_function, unicode_literals

import argparse
import sys


sizeof = {"float": 4, "at::Half": 2, "uint8_t": 1}


def unroll(uf, IndexType, InType, OutType, use_weights, isa, fused):
    def compute(regid, InType, use_weights, isa, prefetch):
        code = []

        if InType == "float":
            code.append(
                "        vop%d = _mm256_fmadd_ps(vwgt, _mm256_loadu_ps(ip + (%d)), vop%d);"  # noqa
                % (regid, regid, regid)
            )
        elif InType == "at::Half":
            code.append(
                "        vop%d = _mm256_fmadd_ps(\n"
                "            vwgt,\n"
                "            _mm256_cvtph_ps(\n"
                "                _mm_loadu_si128(reinterpret_cast<const __m128i*>(ip + (%d)))),\n"  # noqa
                "            vop%d);" % (regid, regid, regid)
            )
        elif InType == "uint8_t":
            code.append(
                "        vop%d = _mm256_fmadd_ps(\n"
                "            vwgt,\n"
                "            _mm256_cvtepi32_ps(_mm256_cvtepu8_epi32(\n"
                "                _mm_loadl_epi64(reinterpret_cast<const __m128i*>(ip + (%d))))),\n"  # noqa
                "            _mm256_add_ps(vop%d, vbio));" % (regid, regid, regid)
            )
        else:
            assert False

        if prefetch:
            code.append(
                "        _mm_prefetch(\n"
                "            reinterpret_cast<const char*>(&ip_next_T0[%d]), _MM_HINT_T0);"
                % (regid)
            )
        else:
            code.append(
                "        // skip unnecessary prefetch of (&ip_next_T0[%d])" % (regid)
            )

        return code

    code = []
    code.append("    // unrolling " + str(uf) + " times")
    code.append("    " + IndexType + " dataInd = 0;")
    code.append(
        "    for ("
        + IndexType
        + " rangeIndex = 0; rangeIndex < output_size; ++rangeIndex) {"
    )
    code.append("      " + OutType + "* op = &out[rangeIndex * block_size];")
    for i in range(0, uf):
        j = 8 * i
        code.append("      __m256 vop" + str(j) + " = _mm256_setzero_ps();")

    # inner loop
    code.append(
        "      for ("
        + IndexType
        + " start = dataInd; dataInd < start + lengths[rangeIndex];\n           ++dataInd) {"  # noqa
    )
    code.append("        const " + IndexType + " idx = indices[dataInd];")
    code.append(
        '        CAFFE_ENFORCE(\n            idx >= 0 && idx < data_size,\n            "Index ",\n            dataInd,\n'  # noqa
        '            " is out of bounds: ",\n            idx,\n            ", range 0 to ",\n            data_size);'  # noqa
    )

    if InType == "uint8_t":
        code.append("        " + OutType + " wgt = 1.f;")
        code.append("        " + OutType + " bio;")
        code.append("        if (weights) {")
        code.append(
            "          wgt = weights[IS_WEIGHT_POSITIONAL ? (dataInd - start) : dataInd];"  # noqa
        )
        code.append("        }")
        if fused:
            code.append(
                "        const float* scale_bias = reinterpret_cast<const float*>(\n"
                "            &input[idx * fused_block_size + block_size]);"
            )
            code.append("        bio = wgt * scale_bias[1];")
            code.append("        wgt = wgt * scale_bias[0];")
        else:
            code.append("        bio = wgt * scale_bias[2 * idx + 1];")
            code.append("        wgt = wgt * scale_bias[2 * idx];")
        code.append("        __m256 vbio = _mm256_set1_ps(bio);")
    else:
        code.append("        " + OutType + " wgt = 1.f;")
        code.append("        if (weights) {")
        code.append(
            "          wgt = weights[IS_WEIGHT_POSITIONAL ? (dataInd - start) : dataInd];"  # noqa
        )
        code.append("        }")
    code.append("        __m256 vwgt = _mm256_set1_ps(wgt);")

    code.append("        const {}* ip = &input[idx * fused_block_size];".format(InType))
    code.append(
        "        const {} next_T0 = (dataInd < index_size - prefdist_T0)\n"
        "            ? (dataInd + prefdist_T0)\n            : dataInd;".format(
            IndexType
        )
    )
    code.append("        const " + IndexType + " idx_pref_T0 = indices[next_T0];")
    code.append("        CAFFE_ENFORCE(idx_pref_T0 >= 0 && idx_pref_T0 < data_size);")

    code.append(
        "        const {}* ip_next_T0 = &input[idx_pref_T0"
        " * fused_block_size];".format(InType)
    )

    for i in range(0, uf):
        j = 8 * i
        cachelinesize = 64
        byteoffset = sizeof[InType] * j
        prefetch = (byteoffset % cachelinesize) == 0
        code.extend(compute(j, InType, use_weights, isa, prefetch))
    code.append("      }")

    code.append("      if (!normalize_by_lengths || lengths[rangeIndex] == 0) {")
    for i in range(0, uf):
        j = 8 * i
        code.append("        _mm256_storeu_ps(&op[" + str(j) + "], vop" + str(j) + ");")
    code.append("      } else {")
    # inv of length
    code.append("        __m256 vlen_inv = _mm256_set1_ps(1.0f / lengths[rangeIndex]);")
    for i in range(0, uf):
        j = 8 * i
        code.append(
            "        _mm256_storeu_ps(&op["
            + str(j)
            + "], _mm256_mul_ps("
            + "vop"
            + str(j)
            + ", vlen_inv));"
        )
    code.append("      }")

    code.append("    }")
    return code


def generic(IndexType, InType, OutType, use_weights, isa, fused):
    def compute(InType, use_weights, isa):
        code = []
        if InType == "float":
            code.append(
                "          _mm256_storeu_ps(\n"
                "              &op[j],\n"
                "              _mm256_fmadd_ps(\n"
                "                  vwgt, _mm256_loadu_ps(&ip[j]), _mm256_loadu_ps(&op[j])));"  # noqa
            )
        elif InType == "at::Half":
            code.append(
                "          _mm256_storeu_ps(\n"
                "              &op[j],\n"
                "              _mm256_fmadd_ps(\n"
                "                  vwgt,\n"
                "                  _mm256_cvtph_ps(_mm_loadu_si128(\n"
                "                      reinterpret_cast<const __m128i*>(&ip[j]))),\n"
                "                  _mm256_loadu_ps(&op[j])));"
            )
        elif InType == "uint8_t":
            code.append(
                "          _mm256_storeu_ps(\n"
                "              &op[j],\n"
                "              _mm256_fmadd_ps(\n"
                "                  vwgt,\n"
                "                  _mm256_cvtepi32_ps(_mm256_cvtepu8_epi32(_mm_loadl_epi64(\n"  # noqa
                "                      reinterpret_cast<const __m128i*>(&ip[j])))),\n"
                "                  _mm256_add_ps(_mm256_loadu_ps(&op[j]), vbio)));"
            )
        else:
            assert False

        code.append(
            "          _mm_prefetch(\n"
            "              reinterpret_cast<const char*>(&ip_next_T0[j]), _MM_HINT_T0);"
        )

        return code

    code = []
    code.append("    " + IndexType + " dataInd = 0;")
    code.append(
        "    for ("
        + IndexType
        + " rangeIndex = 0; rangeIndex < output_size; ++rangeIndex) {"
    )
    code.append("      " + OutType + "* op = &out[rangeIndex * block_size];")

    # initialize to 0
    code.append("      int64_t j = 0;")
    code.append("      for (; j + 8 <= block_size; j += 8) {")
    code.append("        _mm256_storeu_ps(op + j, _mm256_setzero_ps());")
    code.append("      }")
    code.append("      for (; j < block_size; j++) {")
    code.append("        op[j] = 0.0f;")
    code.append("      }")

    # inner loop
    code.append(
        "      for ("
        + IndexType
        + " start = dataInd; dataInd < start + lengths[rangeIndex];\n           ++dataInd) {"  # noqa
    )
    code.append("        const " + IndexType + " idx = indices[dataInd];")
    code.append(
        '        CAFFE_ENFORCE(\n            idx >= 0 && idx < data_size,\n            "Index ",\n            dataInd,\n'  # noqa
        + '            " is out of bounds: ",\n            idx,\n            ", range 0 to ",\n            data_size);'  # noqa
    )

    if InType == "uint8_t":
        code.append("        " + OutType + " wgt = 1.f;")
        code.append("        " + OutType + " bio;")
        code.append("        if (weights) {")
        code.append(
            "          wgt = weights[IS_WEIGHT_POSITIONAL ? (dataInd - start) : dataInd];"  # noqa
        )
        code.append("        }")
        if fused:
            code.append(
                "        const float* scale_bias = reinterpret_cast<const float*>(\n"
                "            &input[idx * fused_block_size + block_size]);"
            )
            code.append("        bio = wgt * scale_bias[1];")
            code.append("        wgt = wgt * scale_bias[0];")
        else:
            code.append("        assert(scale_bias);")
            code.append("        bio = wgt * scale_bias[2 * idx + 1];")
            code.append("        wgt = wgt * scale_bias[2 * idx];")
        code.append("        __m256 vbio = _mm256_set1_ps(bio);")
    else:
        code.append("        " + OutType + " wgt = 1.f;")
        code.append("        if (weights) {")
        code.append(
            "          wgt = weights[IS_WEIGHT_POSITIONAL ? (dataInd - start) : dataInd];"  # noqa
        )
        code.append("        }")
    code.append("        __m256 vwgt = _mm256_set1_ps(wgt);")

    code.append("        const {}* ip = &input[idx * fused_block_size];".format(InType))
    code.append(
        "        const {} next_T0 = (dataInd < index_size - prefdist_T0)\n"
        "            ? (dataInd + prefdist_T0)\n            : dataInd;".format(
            IndexType
        )
    )
    code.append("        const " + IndexType + " idx_pref_T0 = indices[next_T0];")
    code.append("        CAFFE_ENFORCE(idx_pref_T0 >= 0 && idx_pref_T0 < data_size);")
    code.append(
        "        const {}* ip_next_T0 = &input[idx_pref_T0 * fused_block_size];".format(
            InType
        )
    )

    # compute and store main loop
    code.append("        j = 0;")
    code.append("        for (; j + 8 <= block_size; j += 8) {")
    code.extend(compute(InType, use_weights, isa))
    code.append("        }")
    # leftover
    if InType == "at::Half":
        code.append("        alignas(64) at::Half vtmp1[8];")
    code.append("        for (; j < block_size; j++) {")
    if InType == "float":
        code.append("          op[j] += wgt * ip[j];")
    elif InType == "at::Half":
        code.append("          vtmp1[0] = ip[j];")
        code.append("          __m256 vtmp2 = _mm256_cvtph_ps(*((__m128i*)vtmp1));")
        code.append("          op[j] += wgt * ((float*)(&vtmp2))[0];")
    elif InType == "uint8_t":
        code.append("          op[j] += wgt * ((float)ip[j]) + bio;")
    else:
        assert False

    code.append("        }")

    code.append("      }")

    code.append("      if (normalize_by_lengths && lengths[rangeIndex]) {")
    code.append("        float len_inv = 1.0f / lengths[rangeIndex];")
    code.append("        __m256 vlen_inv = _mm256_set1_ps(len_inv);")
    code.append("        j = 0;")
    code.append("        for (; j + 8 <= block_size; j += 8) {")
    code.append(
        "          _mm256_storeu_ps(\n"
        "              &op[j], _mm256_mul_ps(_mm256_loadu_ps(&op[j]), vlen_inv));"
    )
    code.append("        }")
    code.append("        for (; j < block_size; j++) {")
    code.append("          op[j] = len_inv * op[j];")
    code.append("        }")

    code.append("      }")

    code.append("    }")
    return code


# start main code
parser = argparse.ArgumentParser()
parser.add_argument("-f", "--filename", help="file name")
parser.add_argument("--fused", action="store_true")
opts = parser.parse_args()
if opts.filename:
    filename = opts.filename
elif opts.fused:
    filename = "embedding_lookup_fused_8bit_rowwise_avx2.cc"
else:
    filename = "embedding_lookup_avx2.cc"

options = [
    ["int32_t", "int32_t", "float", "float", "float", "float"],
    ["int64_t", "int64_t", "float", "float", "float", "float"],
    ["int32_t", "int32_t", "half", "at::Half", "float", "float"],
    ["int64_t", "int64_t", "half", "at::Half", "float", "float"],
    ["int32_t", "int32_t", "uint8_t", "uint8_t", "float", "float"],
    ["int64_t", "int64_t", "uint8_t", "uint8_t", "float", "float"],
]

code = []
# includes
code.append("//// --------------------------")
code.append("//// ATTENTION:")
code.append("//// THIS CODE IS AUTOGENERATED")
code.append("//// BY {}".format(sys.argv[0]))
code.append("//// DO NOT MODIFY!!!")
code.append("//// --------------------------\n")

code.append("#include <ATen/core/Half.h>")
code.append("#include <c10/util/Logging.h>")
code.append("#include <immintrin.h>")
code.append("#include <cassert>\n")

code.append("namespace caffe2 {\n")
for o in options:
    [IndexTypeName, IndexType, InTypeName, InType, OutTypeName, OutType] = o

    prefix = "Fused8BitRowwise" if opts.fused else ""
    code.append("template <bool IS_WEIGHT_POSITIONAL>")
    fn_base = "{}EmbeddingLookup_{}_{}_{}".format(
        prefix, IndexTypeName, InTypeName, OutTypeName
    )
    suffix = "__avx2_fma"
    fn = "static void " + fn_base + suffix
    code.append(fn + "(")

    args = []
    args.append("    const int64_t block_size,")
    args.append("    const int64_t output_size,")
    args.append("    const int64_t index_size,")
    args.append("    const int64_t data_size,")
    args.append("    const " + InType + "* input,")
    args.append("    const " + IndexType + "* indices,")
    args.append("    const int* lengths,")
    args.append("    const float* weights,")
    if not opts.fused:
        args.append("    const float* scale_bias,")
    args.append("    bool normalize_by_lengths,")
    args.append("    " + OutType + "* out) {")
    code += args

    code.append("  const " + IndexType + " prefdist_T0 = 16;")
    # block_size is the number of elements and fused_block_size is the size of
    # an entire row, including scale and bias.
    offset = (8 // sizeof[InType]) if opts.fused else 0
    code.append(
        "  const {} fused_block_size = block_size + {};".format(IndexType, offset)
    )

    # code.append("printf(\"calling " + fn + "\\n\");");
    if not opts.fused:
        if InType != "uint8_t":
            code.append(
                "  CAFFE_ENFORCE(scale_bias == nullptr,"
                ' "scale_bias must be nullptr");'
            )
        else:
            code.append(
                "  CAFFE_ENFORCE(scale_bias != nullptr,"
                ' "scale_bias must not be nullptr");'
            )

    code.append("  if (block_size == 128) {")
    code += unroll(16, IndexType, InType, OutType, True, "AVX2", opts.fused)
    code.append("  } else if (block_size == 64) {")
    code += unroll(8, IndexType, InType, OutType, True, "AVX2", opts.fused)
    code.append("  } else if (block_size == 32) {")
    code += unroll(4, IndexType, InType, OutType, True, "AVX2", opts.fused)
    code.append("  } else if (block_size == 16) {")
    code += unroll(2, IndexType, InType, OutType, True, "AVX2", opts.fused)
    code.append("  } else {")
    code.append("    // generic code")
    code += generic(IndexType, InType, OutType, True, "AVX2", opts.fused)
    code.append("  }")

    code.append("}")

    for is_weight_positional in ["false", "true"]:
        code.append("void " + fn_base + "_" + is_weight_positional + suffix + "(")
        code += args
        code.append("  " + fn_base + suffix + "<" + is_weight_positional + ">(")
        code.append("      block_size,")
        code.append("      output_size,")
        code.append("      index_size,")
        code.append("      data_size,")
        code.append("      input,")
        code.append("      indices,")
        code.append("      lengths,")
        code.append("      weights,")
        if not opts.fused:
            code.append("      scale_bias,")
        code.append("      normalize_by_lengths,")
        code.append("      out);")
        code.append("}")

    code.append("")

code.append("} // namespace caffe2")

with open(filename, "w") as fout:
    for c in code:
        # print(c, file = fout)
        fout.write(c + "\n")


print("Created " + filename)
