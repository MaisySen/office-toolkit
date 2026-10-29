"""The release signing patch applied to the expo prebuild output."""
import importlib.util
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# The shape expo prebuild (SDK 52) generates for android/app/build.gradle.
TEMPLATE = """android {
    defaultConfig {
        versionName "0.1.0"
    }
    signingConfigs {
        debug {
            storeFile file('debug.keystore')
            storePassword 'android'
            keyAlias 'androiddebugkey'
            keyPassword 'android'
        }
    }
    buildTypes {
        debug {
            signingConfig signingConfigs.debug
        }
        release {
            // Caution! In production, you need to generate your own keystore file.
            // see https://reactnative.dev/docs/signed-apk-android.
            signingConfig signingConfigs.debug
            shrinkResources (findProperty('x')?.toBoolean() ?: false)
            minifyEnabled enableProguardInReleaseBuilds
        }
    }
    packagingOptions {
        jniLibs {
            useLegacyPackaging false
        }
    }
}
"""


def _mod():
    spec = importlib.util.spec_from_file_location(
        "android_upload_signing", ROOT / "scripts" / "android_upload_signing.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _release_block(src):
    start = src.index("        release {")
    return src[start:src.index("\n        }", start)]


def test_release_signs_with_upload_only():
    out = _mod().apply(TEMPLATE)
    release = _release_block(out)
    assert "signingConfig signingConfigs.upload" in release
    assert "signingConfigs.debug" not in release
    assert release.count("signingConfig ") == 1


def test_debug_build_type_keeps_debug_signing():
    out = _mod().apply(TEMPLATE)
    debug = out[out.index("        debug {\n            signingConfig"):]
    assert debug.splitlines()[1].strip() == "signingConfig signingConfigs.debug"


def test_upload_config_sits_inside_the_existing_block():
    out = _mod().apply(TEMPLATE)
    assert out.count("signingConfigs {") == 1
    block = out[out.index("    signingConfigs {"):out.index("    buildTypes {")]
    assert "upload {" in block and "debug {" in block
    assert 'System.getenv("TOOLKIT_UPLOAD_STORE_PASSWORD")' in block


def test_apply_is_idempotent():
    mod = _mod()
    once = mod.apply(TEMPLATE)
    assert mod.apply(once) == once


def test_cli_patches_the_given_file(tmp_path):
    gradle = tmp_path / "build.gradle"
    gradle.write_text(TEMPLATE)
    subprocess.run([sys.executable, str(ROOT / "scripts" / "android_upload_signing.py"),
                    str(gradle)], check=True, capture_output=True)
    assert "signingConfigs.upload" in _release_block(gradle.read_text())
