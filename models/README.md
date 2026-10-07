# Optional face detector

Face detection falls back to OpenCV's bundled Haar cascade, which needs nothing
installed and works offline. It is frontal-only, so a creator at an angle or a
dark webcam can be missed.

For better detection, download YuNet and put it here:

    face_detection_yunet_2023mar.onnx

From https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet

It is picked up automatically on the next run — no code change, no restart flag.
