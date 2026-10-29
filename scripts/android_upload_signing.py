"""Add the Play upload signing config to the generated Android project.

expo prebuild regenerates android/ from the template on every run, so
signing cannot live in the repo copy of build.gradle. Run this after
prebuild: it inserts an `upload` signing config fed by environment
variables and applies it to the release build type. Missing variables
fail the release build loudly instead of silently shipping a debug
signature.
"""
import os
import sys

GRADLE = "android/app/build.gradle"
MARKER = "office-toolkit-upload-signing"

src = open(GRADLE).read()
if MARKER in src:
    print("signing config already applied")
    sys.exit(0)

config = f"""
    // {MARKER}
    signingConfigs {{
        upload {{
            storeFile file(System.getenv("TOOLKIT_UPLOAD_STORE_FILE") ?: "missing-upload-keystore")
            storePassword System.getenv("TOOLKIT_UPLOAD_STORE_PASSWORD")
            keyAlias System.getenv("TOOLKIT_UPLOAD_KEY_ALIAS") ?: "upload"
            keyPassword System.getenv("TOOLKIT_UPLOAD_KEY_PASSWORD")
        }}
    }}
"""
anchor = "    signingConfigs {"
idx = src.index(anchor)
line_end = src.index("\n", idx) + 1
out = src[:line_end] + config + src[line_end:]
release_anchor = "        release {"
ridx = out.index(release_anchor)
rline_end = out.index("\n", ridx) + 1
out = out[:rline_end] + "            signingConfig signingConfigs.upload\n" + out[rline_end:]
open(GRADLE, "w").write(out)
print("upload signing config applied")
