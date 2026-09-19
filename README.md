

# Key components:
1. Scrcpy
2. ADB -
3. MYScrcpy
4. Ultralytics YOLO
5. Roboflow - 
6. PyTorch - 
7. Typesafe AI Jev - 


# 1. Using Scrcpy for screen mirroring 

https://github.com/Genymobile/scrcpy

`brew install scrcpy`
`scrcpy`

Note this is only for screen mirror and manual control. Not script based control.


# 2. Setting up ADB

- install ADB
`brew install android-platform-tools`

- Confirm installation
`adb version`

- Check for connected phone
`adb devices`

# 3. Scripting
`python -m venv .venv`
`source .venv/bin/activate`
`deactivate`


Using MYScrcpy for scripting
https://github.com/me2sy/MYScrcpy/blob/main/README_EN.md

pip install mysc
pip install pyflac

# Recording

adb devices

```
scrcpy \
--no-audio \
--video-codec=h264 \
--video-bit-rate=16M \
--max-fps=30 \
--record=recordings/gameplay-01.mp4
```