# Model files

Large GGUF files are supplied separately; this repository contains the policy,
gateway, launch scripts, integrations and evaluation evidence.

For the existing local installation, preserve:

- `Barq-27B-PQ2_0-v4.gguf`
- `Barq-27B-mmproj-Q8_0.gguf`

For a new installation obtain the PQ2_0 model and Q8_0 projector from the
[upstream model files](https://huggingface.co/prism-ml/Ternary-Bonsai-2-27B-gguf/tree/main),
then use `tools/Prepare-Barq.py` as described in the main documentation. That
tool embeds the current Barq policy in a **new** GGUF, validates tensor payload
hashes, and generates the launch manifest. It does not load the model or train weights.
Do not merely rename an upstream file: the embedded policy must match the gateway.

The recorded evaluated model is 7,206,182,912 bytes (about 6.71 GiB).
The projector is about 0.59 GiB. Model-file size is not total inference memory;
context cache, compute buffers, runtime and host applications also use memory.
