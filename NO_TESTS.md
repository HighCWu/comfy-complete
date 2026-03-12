# Nodes Without Tests

This file documents custom nodes that could not be tested automatically and the reasons why.

## audio-separation-nodes-comfyui

| Node | Reason |
|------|--------|
| AudioSeparation | Requires GPU (uses demucs model for audio source separation) |
| AudioVideoCombine | Requires an external video file path as input |

## comfyui-animatediff-evolved

| Node | Reason |
|------|--------|
| ADE_AnimateDiffLoaderGen1 | Requires motion model files to be downloaded |
| ADE_AnimateDiffLoRALoader | Requires LoRA motion model files |
| ADE_ApplyAnimateDiffModel | Requires MOTION_MODEL_ADE input (from loader) |
| ADE_ApplyAnimateDiffModelSimple | Requires MOTION_MODEL_ADE input (from loader) |
| ADE_UseEvolvedSampling | Requires full AnimateDiff pipeline |
| Most other ADE_* nodes | Many require motion models, VAE, CLIP, or other model-dependent inputs |

## ComfyUI_AudioTools

| Node | Reason |
|------|--------|
| TextToSpeech | Requires TTS model |
| AudioStemSeparate | Requires AI model (htdemucs) |
| AudioSpeechDenoise | Requires AI model (htdemucs) |
| AudioSpeechToTextWhisper | Requires Whisper model |
| AudioLoadBatch | Requires external audio files |
| AudioGetFromList | Requires AUDIO_LIST input |
| AudioRemoveSilence | Tested but empty audio has no silence to remove |
| AudioDisplayWaveform | Visual output only |
| AudioCompareWaveforms | Visual output only |
| AudioLoudnessMeter | Returns LUFS values, difficult to validate |
| AudioBPMDetector | Returns tempo info, difficult to validate without known tempo |
| AudioReactiveParam | Returns envelope data, difficult to validate |
| AudioParametricEQ | Not tested due to complex parameters |
| AudioVocalCompressor | Not tested due to complex parameters |
| AudioDeHum | Not tested due to complex parameters |
| AudioNoiseGate | Not tested due to complex parameters |
| AudioPitchTime | Not tested due to complex parameters |

## comfyui_controlnet_aux

| Node | Reason |
|------|--------|
| DepthAnything* | Requires AI model |
| LeReS* | Requires AI model |
| Midas* | Requires AI model |
| Zoe* | Requires AI model |
| NormalBae* | Requires AI model |
| OpenPose* | Requires AI model |
| DWPose* | Requires AI model |
| MediaPipe* | Requires AI model |
| LineArt* | Requires AI model (except algorithmic variants) |
| HED* | Requires AI model |
| PiDiNet* | Requires AI model |
| MLSD* | Requires AI model |
| SAM/GroundingDINO | Requires AI model |
| Most other preprocessors | Many require downloading pretrained models |

## comfyui-depthanythingv2

| Node | Reason |
|------|--------|
| DepthAnythingV2Preprocessor | Requires DepthAnythingV2 model download |

## comfyui-enricos-nodes

| Node | Reason |
|------|--------|
| Compositor3 | Requires complex COMPOSITOR_CONFIG setup with web frontend interaction |
| CompositorConfig3 | Requires multiple images and masks input |
| CompositorTools3 | Experimental node, needs page reload |
| CompositorTransformsOutV3 | Requires transforms string from Compositor |
| CompositorMasksOutputV3 | Requires layer_outputs from Compositor |
| CompositorColorPicker | Tested |
| ImageColorSampler | Tested |

## comfyui-florence2

| Node | Reason |
|------|--------|
| Florence2* | All nodes require downloading Florence2 model |

## comfyui_essentials

Many image and mask processing nodes were tested. The following require special inputs:
| Node | Reason |
|------|--------|
| ImageApplyLUT_ | Requires LUT file |
| ImageColorMatch_/ImageHistogramMatch_ | Requires reference image |
| ImageRemoveBackground_ | Requires REMBG_SESSION |
| Segmentation nodes | Require AI models |

## comfyui-fl-path-animator

| Node | Reason |
|------|--------|
| Most nodes | Require complex path data or video inputs |

## comfyui-impact-pack

| Node | Reason |
|------|--------|
| Detectors | Require AI models (YOLO, SAM, etc.) |
| Segmentation nodes | Require AI models |
| Face analysis | Require AI models |
| Many utility nodes | Could be tested with more time |

## comfyui_ipadapter_plus

| Node | Reason |
|------|--------|
| All IPAdapter nodes | Require IPAdapter model files |

## comfyui-kjnodes

Many utility nodes could be tested. Some require:
| Node | Reason |
|------|--------|
| Model-related nodes | Require models |
| Scheduling nodes | May need complex setup |

## ComfyUI-Lotus

| Node | Reason |
|------|--------|
| All nodes | Require Lotus depth model |

## ComfyUI-Manager

| Node | Reason |
|------|--------|
| Manager nodes | Require package management operations |

## ComfyUI-NormalCrafterWrapper

| Node | Reason |
|------|--------|
| NormalCrafter nodes | Require NormalCrafter model |

## comfyui-segment-anything-2

| Node | Reason |
|------|--------|
| SAM2 nodes | Require SAM2 model |

## comfyui-supir

| Node | Reason |
|------|--------|
| SUPIR nodes | Require SUPIR upscaler model |

## comfyui_ultimatesdupscale

| Node | Reason |
|------|--------|
| Upscale nodes | Require upscaler models and complex tiling setup |

## comfyui-videohelpersuite

| Node | Reason |
|------|--------|
| Video load nodes | Require video files |
| Video combine nodes | Tested with VHS_* prefix |

## ComfyUI-WanAnimatePreprocess

| Node | Reason |
|------|--------|
| All nodes | Require video preprocessing models |

## ComfyUI-WanVideoWrapper

| Node | Reason |
|------|--------|
| All nodes | Require Wan video generation models |
