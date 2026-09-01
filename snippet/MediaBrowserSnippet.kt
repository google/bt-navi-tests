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

import android.content.ComponentName
import android.os.Bundle
import android.os.Handler
import android.util.Log
import androidx.media3.session.legacy.MediaBrowserCompat
import androidx.media3.session.legacy.MediaControllerCompat
import androidx.media3.session.legacy.MediaDescriptionCompat
import androidx.media3.session.legacy.MediaMetadataCompat
import androidx.media3.session.legacy.PlaybackStateCompat
import androidx.test.platform.app.InstrumentationRegistry
import com.google.android.mobly.snippet.Snippet
import com.google.android.mobly.snippet.rpc.AsyncRpc
import com.google.android.mobly.snippet.rpc.Rpc
import com.google.android.mobly.snippet.rpc.RunOnUiThread
import java.util.UUID
import kotlin.time.Duration.Companion.seconds
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeoutOrNull

/** MediaBrowser snippet for testing. */
class MediaBrowserSnippet : Snippet {
  private val instrumentation = InstrumentationRegistry.getInstrumentation()
  private val context = instrumentation.targetContext
  internal val browsers = mutableMapOf<String, Pair<MediaBrowserCompat, MediaControllerCompat>>()
  private val handler = Handler(context.mainLooper)

  private fun getMediaController(cookie: String): MediaControllerCompat =
    browsers[cookie]?.second
      ?: throw IllegalArgumentException("Browser with $cookie doesn't exist!")

  private fun getMediaBrowser(cookie: String): MediaBrowserCompat =
    browsers[cookie]?.first ?: throw IllegalArgumentException("Browser with $cookie doesn't exist!")

  /** Connects to a media browser */
  @Rpc(description = "Connect to a media browser")
  fun connectMediaBrowser(packageName: String, serviceClassName: String): String {
    val deferred = CompletableDeferred<Unit>()
    lateinit var browser: MediaBrowserCompat
    context.mainExecutor.execute {
      browser =
        MediaBrowserCompat(
          context,
          ComponentName.createRelative(packageName, serviceClassName),
          object : MediaBrowserCompat.ConnectionCallback() {
            override fun onConnected() {
              deferred.complete(Unit)
            }

            override fun onConnectionFailed() {
              deferred.completeExceptionally(
                IllegalStateException("Failed to connect to media browser")
              )
            }
          },
          null,
        )
      browser.connect()
    }
    runBlocking { withTimeoutOrNull(TIMEOUT) { deferred.await() } }
      ?: throw IllegalStateException("Timeout connecting to media browser")
    val cookie = UUID.randomUUID().toString()
    browsers[cookie] = browser to MediaControllerCompat(context, browser.sessionToken)
    return cookie
  }

  /** Disconnects a media browser. */
  @RunOnUiThread
  @Rpc(description = "Disconnect a media browser")
  fun disconnectMediaBrowser(cookie: String) {
    browsers.remove(cookie)?.first?.let { it.disconnect() }
  }

  /** Get the root media browser item. */
  @Rpc(description = "Get the root media browser item")
  fun getMediaBrowserRootId(cookie: String): String = getMediaBrowser(cookie).root

  private fun isDownloading(bundle: Bundle?): Boolean {
    if (bundle == null) return false
    if (bundle.containsKey(MediaBrowserCompat.EXTRA_DOWNLOAD_PROGRESS)) {
      val progress = bundle.getFloat(MediaBrowserCompat.EXTRA_DOWNLOAD_PROGRESS)
      Log.d(TAG, "isDownloading: EXTRA_DOWNLOAD_PROGRESS: $progress")
      return true
    }
    if (bundle.containsKey(MediaDescriptionCompat.EXTRA_DOWNLOAD_STATUS)) {
      val status = bundle.getLong(MediaDescriptionCompat.EXTRA_DOWNLOAD_STATUS)
      Log.d(TAG, "isDownloading: EXTRA_DOWNLOAD_STATUS: $status")
      if (status == MediaDescriptionCompat.STATUS_DOWNLOADING) return true
    }
    return false
  }

  private fun isDownloading(item: MediaBrowserCompat.MediaItem): Boolean =
    isDownloading(item.description.extras)

  /** Get the children of a media browser item. */
  @Rpc(description = "Get the children of a media browser item")
  fun getMediaBrowserChildren(cookie: String, mediaId: String): List<MediaBrowserCompat.MediaItem> {
    val browser = getMediaBrowser(cookie)
    val deferred = CompletableDeferred<List<MediaBrowserCompat.MediaItem>?>()

    val callback =
      object : MediaBrowserCompat.SubscriptionCallback() {
        override fun onChildrenLoaded(
          parentId: String?,
          children: List<MediaBrowserCompat.MediaItem>?,
          options: Bundle?,
        ) {
          if (isDownloading(options) || children?.any { isDownloading(it) } == true) {
            Log.d(TAG, "Skipping DOWNLOAD_PENDING children for $parentId")
            return
          }
          children?.let { deferred.complete(children) }
            ?: deferred.completeExceptionally(
              IllegalStateException("Got null children for $parentId")
            )
        }

        override fun onError(parentId: String?, options: Bundle?) {
          deferred.completeExceptionally(
            IllegalStateException("Failed to get children for $parentId")
          )
        }
      }
    val result: List<MediaBrowserCompat.MediaItem>
    try {
      browser.subscribe(mediaId, Bundle(), callback)
      result =
        runBlocking { withTimeoutOrNull(TIMEOUT) { deferred.await() } }
          ?: throw IllegalStateException("Timeout getting children for $mediaId")
    } finally {
      browser.unsubscribe(mediaId, callback)
    }
    return result
  }

  /** Register a media controller callback. */
  @RunOnUiThread
  @AsyncRpc(description = "Register a media controller callback")
  fun registerMediaControllerCallback(callbackId: String, cookie: String) {
    val callback =
      object : MediaControllerCompat.Callback() {
        override fun onPlaybackStateChanged(state: PlaybackStateCompat?) {
          Utils.postSnippetEvent(
            callbackId,
            SnippetConstants.MEDIA_CONTROLLER_PLAYBACK_STATE_CHANGE,
          ) {
            putInt(SnippetConstants.FIELD_STATE, state?.state ?: PlaybackStateCompat.STATE_ERROR)
          }
        }

        override fun onMetadataChanged(metadata: MediaMetadataCompat?) {
          Utils.postSnippetEvent(callbackId, SnippetConstants.MEDIA_CONTROLLER_METADATA_CHANGE) {
            putString(
              SnippetConstants.FIELD_TITLE,
              metadata?.getString(MediaMetadataCompat.METADATA_KEY_TITLE),
            )
            putString(
              SnippetConstants.FIELD_ARTIST,
              metadata?.getString(MediaMetadataCompat.METADATA_KEY_ARTIST),
            )
            putString(
              SnippetConstants.FIELD_ALBUM,
              metadata?.getString(MediaMetadataCompat.METADATA_KEY_ALBUM),
            )
          }
        }

        override fun onRepeatModeChanged(repeatMode: Int) {
          Utils.postSnippetEvent(callbackId, SnippetConstants.PLAYER_REPEAT_MODE_CHANGED) {
            putInt(SnippetConstants.MODE, repeatMode)
          }
        }

        override fun onShuffleModeChanged(shuffleMode: Int) {
          Utils.postSnippetEvent(callbackId, SnippetConstants.PLAYER_SHUFFLE_MODE_ENABLED_CHANGED) {
            putInt(SnippetConstants.MODE, shuffleMode)
          }
        }
      }
    getMediaController(cookie).registerCallback(callback, handler)
  }

  /** Play media controller. */
  @RunOnUiThread
  @Rpc(description = "Play media controller")
  fun playMediaController(cookie: String) {
    getMediaController(cookie).transportControls.play()
  }

  /** Pause media controller. */
  @RunOnUiThread
  @Rpc(description = "Pause media controller")
  fun pauseMediaController(cookie: String) {
    getMediaController(cookie).transportControls.pause()
  }

  /** Stop media controller. */
  @RunOnUiThread
  @Rpc(description = "Stop media controller")
  fun stopMediaController(cookie: String) {
    getMediaController(cookie).transportControls.stop()
  }

  /** Fast forward media controller. */
  @RunOnUiThread
  @Rpc(description = "Fast forward media controller")
  fun fastForwardMediaController(cookie: String) {
    getMediaController(cookie).transportControls.fastForward()
  }

  /** Rewind media controller. */
  @RunOnUiThread
  @Rpc(description = "Rewind media controller")
  fun rewindMediaController(cookie: String) {
    getMediaController(cookie).transportControls.rewind()
  }

  /** Skip to next media controller. */
  @RunOnUiThread
  @Rpc(description = "Skip to next media controller")
  fun skipToNextMediaController(cookie: String) {
    getMediaController(cookie).transportControls.skipToNext()
  }

  /** Skip to previous media controller. */
  @RunOnUiThread
  @Rpc(description = "Skip to previous media controller")
  fun skipToPreviousMediaController(cookie: String) {
    getMediaController(cookie).transportControls.skipToPrevious()
  }

  /** Set repeat mode media controller. */
  @RunOnUiThread
  @Rpc(description = "Set repeat mode media controller")
  fun setMediaControllerRepeatMode(cookie: String, repeatMode: Int) {
    getMediaController(cookie).transportControls.setRepeatMode(repeatMode)
  }

  /** Set shuffle mode media controller. */
  @RunOnUiThread
  @Rpc(description = "Set shuffle mode media controller")
  fun setMediaControllerShuffleMode(cookie: String, shuffleMode: Int) {
    getMediaController(cookie).transportControls.setShuffleMode(shuffleMode)
  }

  private companion object {
    const val TAG = "MediaBrowserSnippet"
    val TIMEOUT = 5.seconds
  }
}
