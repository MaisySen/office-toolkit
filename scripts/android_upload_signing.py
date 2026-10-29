"""Add the Play upload signing config to the generated Android project.

expo prebuild regenerates android/ from the template on every run, so
signing cannot live in the repo copy of build.gradle. Run this after
prebuild (from any directory): it adds an `upload` signing config fed by
environment variables to the template's signingConfigs block and makes
it the release build type's only signing config, replacing the
template's debug signing. Missing variables fail the release build
loudly instead of silently shipping a debug signature.

Usage: python3 scripts/android_upload_signing.py [path/to/app/build.gradle]
"""
import os
import re
import sys

MARKER = "office-toolkit-upload-signing"
DEFAULT_GRADLE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "mobile", "android", "app", "build.gradle")

UPLOAD_CONFIG = f"""        // {MARKER}
        upload {{
            storeFile file(System.getenv("TOOLKIT_UPLOAD_STORE_FILE") ?: "missing-upload-keystore")
            storePassword System.getenv("TOOLKIT_UPLOAD_STORE_PASSWORD")
            keyAlias System.getenv("TOOLKIT_UPLOAD_KEY_ALIAS") ?: "upload"
            keyPassword System.getenv("TOOLKIT_UPLOAD_KEY_PASSWORD")
        }}
"""


def _block_end(src, open_idx):
    """Index just past the brace that closes the block opened at open_idx."""
    depth = 0
    for i in range(open_idx, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    raise ValueError("unbalanced braces in build.gradle")


def apply(src):
    if MARKER in src:
        return src
    # 1. the upload config goes inside the template's signingConfigs block
    m = re.search(r"^    signingConfigs \{\n", src, re.M)
    if not m:
        raise ValueError("no signingConfigs block in build.gradle")
    src = src[:m.end()] + UPLOAD_CONFIG + src[m.end():]
    # 2. the release build type signs with upload and nothing else
    bt = re.search(r"^    buildTypes \{", src, re.M)
    if not bt:
        raise ValueError("no buildTypes block in build.gradle")
    bt_end = _block_end(src, src.index("{", bt.start()))
    rel = re.compile(r"^        release \{", re.M).search(src, bt.start(), bt_end)
    if not rel:
        raise ValueError("no release build type in build.gradle")
    rel_open = src.index("{", rel.start())
    rel_end = _block_end(src, rel_open)
    body = src[rel_open + 1:rel_end - 1]
    body = re.sub(r"^\s*signingConfig\s+signingConfigs\.\w+\s*\n", "", body, flags=re.M)
    body = "\n            signingConfig signingConfigs.upload" + body
    return src[:rel_open + 1] + body + src[rel_end - 1:]


def main(argv):
    gradle = argv[1] if len(argv) > 1 else DEFAULT_GRADLE
    src = open(gradle).read()
    if MARKER in src:
        print("signing config already applied")
        return 0
    out = apply(src)
    open(gradle, "w").write(out)
    print("upload signing config applied to", os.path.normpath(gradle))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
