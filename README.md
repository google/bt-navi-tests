# Navi Bluetooth Test Suite

This is a test suite for Bluetooth on Android devices.

## Prerequisites

* **Environment**

  RF Shielding box is recommended to use. Flakiness rate could be high if the air is not clear.

* **DUT devices**

  Must be an Android device with SDK>=33 (Android 13). All Pixel devices with SDK>=33(Android 13) are supported.

* **REF devices**

  Currently, only **Pixel 8/8a** and later series are officially supported. Other devices may work, but not tested.
  
  Most USB dongles cannot fully support HFP / LE test cases, especially LE Audio.

* **Test host.**

  We recommend to use Linux as the test host, but Windows is also supported.

  The test host should have the following libraries installed:
  * python3.11 or later
    * Check your Python 3 version number:

    ```bash
    python3 --version
    ```

    * If your version is lower than Python 3.12, install the latest version
    following <https://wiki.python.org/moin/BeginnersGuide/Download>.
    * You may also use [uv](https://github.com/astral-sh/uv) to manage the workspace.

    ```bash
    # On Linux & macOS
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # On Windows
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

    uv python install 3.12
    ```

  * ADB
    * If you don't already have the `adb` command-line tool, download and
      install it from
      [Android SDK Platform Tools](https://developer.android.com/tools/releases/platform-tools#downloads).
    * Make sure that the installed binary is in the host's `PATH`, so it can
      be run directly with `adb` in the command line.

  * FFmpeg

    Some of cases need FFmpeg. Please download them from <https://ffmpeg.org/download.html> and add the binary to your PATH.

## Test steps

Follow these steps to prepare and execute tests and review test results.

### Prepare the test

Prepare the following materials to be used for the tests.

#### Get the test repo and install the test environment

```bash
git clone https://github.com/google/bt-navi-tests.git
cd bt-navi-tests

python3 -m venv .venv
python3 -m pip install -e .

# Or use uv
uv venv .venv --python 3.12
uv pip install -e .
```

#### Configure testbed

NOTE: If you don't care the order, use testbed `any`, or let `test_runner` generate one.

Modify the test config file `config.yml` as follows:

* Find device serial numbers:

```bash
$ adb devices -l
List of devices attached

localhost:33461        device product:akita model:Pixel_8a device:akita transport_id:5
localhost:40155        device product:caiman model:Pixel_9_Pro device:caiman transport_id:3
localhost:46879        device product:akita model:Pixel_8a device:akita transport_id:4
```

In this example, the source device is `localhost:33461` and the target
device is `localhost:40155` and `localhost:46879`.

* Specify the target and source device serial numbers:

```yaml
"AndroidDevice": [
    {
    "serial": "localhost:33461",
    "label": "DUT"
    },
    {
    "serial": "localhost:40155",
    "label": "REF"
    },
    {
    "serial": "localhost:46879",
    "label": "REF"
    }
]
```

### Run the test

This test suite has been integrated with [Mobly Android Partner Tools](https://github.com/android/mobly-android-partner-tools/tree/main), so it can be run with

```bash
test_runner generic_functionality_suite -c config.yml -tb default -i [-u]
```

NOTE: If -c and -tb is not specified, Mobly test_runner will automatically select all devices with the order present in `adb devices`. Check `test_runner -h` for more details.

Or you may run the following commands from the local directory, but you need to manually install `bluetooth_snippet.apk` before running the test:

```bash
python3 navi/tests/generic_functionality_suite.py -c config.yml -tb default
```

### Upload test results

After running the test, Mobly should generate a log directory like:

```log
Artifacts are saved in "/tmp/logs/mobly/..."
```

If you have partner GCP account, you can upload the test result to GCP with results_uploader:

```bash
results_uploader /tmp/logs/mobly/...
```
