# Runtime files

Use the exact Prism ternary-compatible build used in the recorded evaluations:
`prism-b10683-d8f26ee`, Windows x64 CUDA 12.4.

1. Download [runtime archive](https://github.com/PrismML-Eng/llama.cpp/releases/download/prism-b10683-d8f26ee/llama-prism-b10683-d8f26ee-bin-win-cuda-12.4-x64.zip).
2. Download [CUDA runtime libraries](https://github.com/PrismML-Eng/llama.cpp/releases/download/prism-b10683-d8f26ee/cudart-llama-bin-win-cuda-12.4-x64.zip).
3. Extract both so `runtime/llama-server.exe` and its companion DLLs are directly in this directory.

Binaries are not included in this source repository. A generic llama.cpp build
is not assumed to support the same PQ2_0 format or template behavior.
Read the [upstream release](https://github.com/PrismML-Eng/llama.cpp/releases/tag/prism-b10683-d8f26ee)
and applicable runtime/vendor licenses. The supplied launcher is Windows-specific.
