/*
 * Copyright 2026 The Android Open Source Project
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package com.google.wireless.android.pixel.bluetooth.snippet

import android.app.Activity
import android.content.Intent
import android.util.Log

class VoiceCommandActivity : Activity() {

  override fun onStart() {
    super.onStart()
    Log.i(TAG, "onStart")
    sendBroadcast(Intent(ACTION_COMMAND).putExtra(EXTRA_INTENT, intent).setPackage(packageName))
    finish()
  }

  companion object {
    const val TAG = "VoiceCommandActivity"
    const val ACTION_COMMAND = "com.google.wireless.android.pixel.bluetooth.snippet.ACTION_COMMAND"
    const val EXTRA_INTENT = "intent"
  }
}
