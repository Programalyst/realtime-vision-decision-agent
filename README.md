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