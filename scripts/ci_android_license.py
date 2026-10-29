"""Pre-acceptance validator for the android-runtime workflow.

Before any license acceptance happens in CI, this checks Google's live
SDK repository metadata and:
  1. resolves the exact stable components the workflow installs
     (channel-0 only; preview channels are a refusal);
  2. asserts every one of them carries ONLY the android-sdk-license
     identifier;
  3. hashes the license text exactly the way sdkmanager does and
     requires it to equal the pinned hash below. sdkmanager computes
     sha1 over the parsed license value (tools/base repository
     License.java getLicenseHash), where parsing applies
     TrimStringAdapter: drop spaces/tabs following any whitespace,
     replace lone newlines with a space, collapse duplicate spaces,
     trim. Verified end-to-end: this hash equals the acceptance hash
     sdkmanager itself writes for this license.

Any mismatch exits non-zero BEFORE sdkmanager is invoked, so no changed
or unexpected terms can ever be accepted. With --write-license-file the
validated pin is written as the pre-recorded acceptance; the workflow
then runs sdkmanager --install with stdin closed, so if the seeded hash
ever failed to match, the install would fail on EOF instead of
prompting - fail-closed in both directions."""
import hashlib
import re
import sys
import urllib.request

SOURCES = [
    "https://dl.google.com/android/repository/repository2-1.xml",
    "https://dl.google.com/android/repository/repository2-3.xml",
    "https://dl.google.com/android/repository/sys-img/google_apis/sys-img2-1.xml",
]
# Pinned hash of the approved license text: the sdkmanager-served
# metadata copy of android-sdk-license after sdkmanager's own
# TrimStringAdapter normalization (verified against its printed text and
# the acceptance hash it writes). Scope: the five stable components in
# android-runtime.yml, CI test setup only. Preview, legacy and any other
# licenses are refused by this gate.
PINNED_SHA1 = "24333f8a63b6825ea9c5514f83c2829b004d1fee"
APPROVED_LICENSE_ID = "android-sdk-license"


def fail(msg):
    print(f"LICENSE-GATE REFUSAL: {msg}")
    sys.exit(1)


def sdkmanager_value(text):
    """The license value sdkmanager computes: TrimStringAdapter."""
    text = re.sub(r"(?<=\s)[ \t]*", "", text)
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    text = re.sub(r" +", " ", text)
    return text.strip()


def main():
    args = sys.argv[1:]
    write_to = None
    if args and args[0].startswith("--write-license-file"):
        flag = args.pop(0)
        if "=" in flag:
            write_to = flag.split("=", 1)[1]
        elif args:
            write_to = args.pop(0)
        if not write_to:
            fail("--write-license-file needs a path")
    components = args
    if not components:
        fail("no components given")
    docs = [urllib.request.urlopen(u, timeout=30).read().decode()
            for u in SOURCES]
    license_texts = {}
    for doc in docs:
        license_texts.update(dict(re.findall(
            r'<license id="([^"]+)"[^>]*>(.*?)</license>', doc, re.S)))
    if APPROVED_LICENSE_ID not in license_texts:
        fail(f"{APPROVED_LICENSE_ID} not present in repository metadata")
    digest = hashlib.sha1(
        sdkmanager_value(license_texts[APPROVED_LICENSE_ID]).encode()
    ).hexdigest()
    if digest != PINNED_SHA1:
        fail(f"license text changed: metadata hashes to {digest}, "
             f"pin is {PINNED_SHA1}. Re-approval required before use.")

    for comp in components:
        blocks = []
        for doc in docs:
            blocks += re.findall(
                r'<remotePackage path="' + re.escape(comp)
                + r'"[^>]*>(.*?)</remotePackage>', doc, re.S)
        stable = [b for b in blocks
                  if 'channelRef ref="channel-0"' in b]
        if not blocks:
            fail(f"component not in metadata: {comp}")
        if not stable:
            fail(f"component {comp} has no stable-channel listing")
        for b in stable:
            refs = re.findall(r'uses-license ref="([^"]+)"', b)
            if refs != [APPROVED_LICENSE_ID]:
                fail(f"component {comp} carries license refs {refs}, "
                     f"expected only {APPROVED_LICENSE_ID}")
        print(f"OK {comp}: stable channel, license {APPROVED_LICENSE_ID}")
    print(f"license {APPROVED_LICENSE_ID} text matches the pinned hash "
          f"{PINNED_SHA1}")
    if write_to:
        import os
        os.makedirs(os.path.dirname(write_to), exist_ok=True)
        with open(write_to, "w") as f:
            f.write(PINNED_SHA1 + "\n")
        print(f"pre-recorded acceptance written to {write_to}")


if __name__ == "__main__":
    main()
