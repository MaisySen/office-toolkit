"""Pre-acceptance validator for the parked android-runtime workflow.

Before any license acceptance happens in CI, this checks Google's live
SDK repository metadata and:
  1. resolves the exact stable components the workflow installs
     (channel-0 only; preview channels are a refusal);
  2. asserts every one of them carries ONLY the android-sdk-license
     identifier;
  3. hashes the license text (trim + collapse whitespace, sha1; this
     normalization is a candidate match for sdkmanager's own hashing and
     is NOT yet proven identical - the install step fails closed if it
     differs) and requires it to equal the approved pinned hash below.
     The owner approved the sdkmanager-served metadata text on
     2026-10-29, scoped to five stable components for CI test setup;
     that text differs in a few sections from the live
     https://developer.android.com/studio/terms page, and the approval
     covers the metadata text, not the page.

Any mismatch exits non-zero BEFORE sdkmanager is invoked, so no changed
or unexpected terms can ever be accepted. The workflow then seeds only
$ANDROID_HOME/licenses/android-sdk-license with that hash and runs the
install with stdin closed: if the seeded hash did not match sdkmanager's
own hashing, the install fails on EOF instead of prompting - fail-closed
in both directions.
"""
import hashlib
import re
import sys
import urllib.request

SOURCES = [
    "https://dl.google.com/android/repository/repository2-1.xml",
    "https://dl.google.com/android/repository/repository2-3.xml",
    "https://dl.google.com/android/repository/sys-img/google_apis/sys-img2-1.xml",
]
# Approved by the owner on 2026-10-29, scoped to the sdkmanager-served
# text below and the five stable components in android-runtime.yml for
# CI test setup only. Preview, legacy and any other licenses remain
# unapproved and are refused by this gate.
PINNED_SHA1 = "a90c249c9e874a0ca1854dd16aa9827dec1430da"
APPROVED_LICENSE_ID = "android-sdk-license"


def fail(msg):
    print(f"LICENSE-GATE REFUSAL: {msg}")
    sys.exit(1)


def normalize(text):
    return re.sub(r"\s+", " ", text).strip()


def main():
    components = sys.argv[1:]
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
        normalize(license_texts[APPROVED_LICENSE_ID]).encode()).hexdigest()
    if digest != PINNED_SHA1:
        fail(f"license text changed: metadata hashes to {digest}, "
             f"pin is {PINNED_SHA1}. Owner re-approval required before use.")

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


if __name__ == "__main__":
    main()
