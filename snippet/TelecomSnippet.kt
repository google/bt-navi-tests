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

import android.annotation.SuppressLint
import android.content.ComponentName
import android.content.ContentProviderOperation
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.IBinder
import android.os.OutcomeReceiver
import android.os.ParcelUuid
import android.provider.CallLog
import android.provider.ContactsContract
import android.provider.Telephony
import android.telecom.Call
import android.telecom.CallAttributes
import android.telecom.CallControl
import android.telecom.CallControlCallback
import android.telecom.CallEndpoint
import android.telecom.CallEventCallback
import android.telecom.CallException
import android.telecom.DisconnectCause
import android.telecom.PhoneAccount
import android.telecom.PhoneAccountHandle
import android.telecom.TelecomManager
import android.util.Log
import androidx.annotation.RequiresApi
import androidx.core.net.toUri
import androidx.test.platform.app.InstrumentationRegistry
import com.google.android.mobly.snippet.Snippet
import com.google.android.mobly.snippet.rpc.AsyncRpc
import com.google.android.mobly.snippet.rpc.Rpc
import com.google.android.mobly.snippet.rpc.RpcOptional
import com.google.wireless.android.pixel.bluetooth.snippet.Utils.postSnippetEvent
import java.util.function.Consumer
import kotlin.time.Duration.Companion.seconds
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.channels.trySendBlocking
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.timeout
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.withTimeoutOrNull
import org.json.JSONArray

/** Snippet related to Telecom. */
@RequiresApi(Build.VERSION_CODES.UPSIDE_DOWN_CAKE)
@SuppressLint("MissingPermission")
class TelecomSnippet : Snippet {
  private val instrumentation = InstrumentationRegistry.getInstrumentation()
  private val context = instrumentation.targetContext
  private val callControls = mutableMapOf<String, CallControl>()
  private var inCallService: InCallServiceImpl
  private val telecomManager = context.getSystemService(TelecomManager::class.java)
  private val availableCallEndpoints = MutableStateFlow<List<CallEndpoint>>(emptyList())

  init {
    instrumentation.uiAutomation.adoptShellPermissionIdentity()

    val flow = callbackFlow {
      val serviceConnection =
        object : ServiceConnection {
          override fun onServiceConnected(name: ComponentName, service: IBinder) {
            val inCallService = (service as InCallServiceImpl.LocalBinder).getService()
            val unused = trySendBlocking(inCallService)
          }

          override fun onServiceDisconnected(name: ComponentName) {
            Log.i(TAG, "onServiceDisconnected: $name")
          }
        }

      // Bind InCallServiceImpl to control calls, without setting it as the default dialer.
      val intent =
        Intent(context, InCallServiceImpl::class.java).apply {
          action = InCallServiceImpl.LOCAL_BINDING_LOCATION
        }
      context.bindService(intent, serviceConnection, Context.BIND_AUTO_CREATE)

      awaitClose()
    }

    runBlocking { inCallService = flow.timeout(3.seconds).first() }
    telecomManager.registerPhoneAccount(PHONE_ACCOUNT)
  }

  /** Registers a callback with [callbackId]. */
  @AsyncRpc(description = "Register Telecom callback")
  fun registerTelecomCallback(callbackId: String) {
    inCallService.callbacks[callbackId] =
      object : InCallServiceImpl.Callback {
        override fun onCallStateChanged(call: Call, state: Int) {
          postSnippetEvent(callbackId, SnippetConstants.CALL_STATE_CHANGED) {
            putString(SnippetConstants.FIELD_NAME, call.details.callerDisplayName)
            putString(SnippetConstants.FIELD_HANDLE, call.details.handle?.toString())
            putInt(SnippetConstants.FIELD_STATE, state)
          }
        }

        override fun onCallRemoved(call: Call) {
          postSnippetEvent(callbackId, SnippetConstants.CALL_REMOVED) {
            putString(SnippetConstants.FIELD_NAME, call.details.callerDisplayName)
            putString(SnippetConstants.FIELD_HANDLE, call.details.handle?.toString())
          }
        }
      }
  }

  /** Unregisters a callback with [callbackId]. */
  @Rpc(description = "Unregister Telecom callback")
  fun unregisterTelecomCallback(callbackId: String) {
    inCallService.callbacks.remove(callbackId)
  }

  /**
   * Adds a new call with [callerName] and [callerAddress], and returns cookie of the new call. If
   * [isIncoming] is true, an incoming call will be placed, otherwise the new call is outgoing.
   */
  @AsyncRpc(description = "Add a call")
  fun addCall(callbackId: String, callerName: String, callerAddress: String, isIncoming: Boolean) {
    val direction =
      if (isIncoming) {
        CallAttributes.DIRECTION_INCOMING
      } else {
        CallAttributes.DIRECTION_OUTGOING
      }
    val cookie = callbackId

    val callControlCallback =
      object : CallControlCallback {
        override fun onAnswer(videoState: Int, wasCompleted: Consumer<Boolean>) {
          Log.i(TAG, "onAnswer cookie=$cookie, videoState=$videoState")
          wasCompleted.accept(cookie in callControls)
        }

        override fun onCallStreamingStarted(wasCompleted: Consumer<Boolean>) {
          Log.i(TAG, "onCallStreamingStarted cookie=$cookie")
          wasCompleted.accept(cookie in callControls)
        }

        override fun onDisconnect(
          disconnectCause: DisconnectCause,
          wasCompleted: Consumer<Boolean>,
        ) {
          Log.i(TAG, "onDisconnect cookie=$cookie, cause=$disconnectCause")
          wasCompleted.accept(callControls.remove(cookie) != null)
        }

        override fun onSetActive(wasCompleted: Consumer<Boolean>) {
          Log.i(TAG, "onSetActive cookie=$cookie")
          wasCompleted.accept(cookie in callControls)
        }

        override fun onSetInactive(wasCompleted: Consumer<Boolean>) {
          wasCompleted.accept(cookie in callControls)
        }
      }

    val callEventCallback =
      object : CallEventCallback {
        override fun onAvailableCallEndpointsChanged(
          availableEndpoints: MutableList<CallEndpoint>
        ) {
          Log.i(
            TAG,
            "onAvailableCallEndpointsChanged cookie=$cookie, endpoints=$availableEndpoints",
          )
          availableCallEndpoints.value = availableEndpoints
        }

        override fun onCallEndpointChanged(newCallEndpoint: CallEndpoint) {
          Log.i(TAG, "onCallEndpointChanged cookie=$cookie, endpoint=$newCallEndpoint")
          postSnippetEvent(callbackId, SnippetConstants.CALL_ENDPOINT_CHANGED) {
            putParcelable(SnippetConstants.FIELD_ENDPOINT, newCallEndpoint)
          }
        }

        override fun onCallStreamingFailed(reason: Int) {
          Log.i(TAG, "onCallStreamingFailed cookie=$cookie, reason=$reason")
        }

        override fun onEvent(event: String, extras: Bundle) {
          Log.i(TAG, "onEvent cookie=$cookie, event=$event")
        }

        override fun onMuteStateChanged(isMuted: Boolean) {
          Log.i(TAG, "onMuteStateChanged cookie=$cookie, isMuted=$isMuted")
        }
      }

    val deferred = DeferredOutcomeReceiver<CallControl, CallException>()
    telecomManager.addCall(
      CallAttributes.Builder(PHONE_ACCOUNT_HANDLE, direction, callerName, callerAddress.toUri())
        .build(),
      context.mainExecutor,
      deferred,
      callControlCallback,
      callEventCallback,
    )
    try {
      val callControl = runBlocking { withTimeout(CALL_CONTROL_TIMEOUT) { deferred.await() } }
      callControls[cookie] = callControl
    } catch (e: TimeoutCancellationException) {
      throw RuntimeException("Add call timeout after $CALL_CONTROL_TIMEOUT", e)
    }
  }

  /** Answers a call with [cookie]. */
  @Rpc(description = "Answer a call")
  fun answerCall(cookie: String) {
    val deferred = DeferredOutcomeReceiver<Void?, CallException>()
    callControls[cookie]?.answer(CallAttributes.AUDIO_CALL, context.mainExecutor, deferred)
      ?: throw IllegalArgumentException("Call with $cookie doesn't exist!")
    try {
      runBlocking { withTimeout(CALL_CONTROL_TIMEOUT) { deferred.await() } }
    } catch (e: TimeoutCancellationException) {
      throw RuntimeException("Answer call timeout after $CALL_CONTROL_TIMEOUT", e)
    }
  }

  /** Disconnects a call with [cookie]. */
  @Rpc(description = "Disconnect a call")
  fun disconnectCall(cookie: String) {
    val deferred = DeferredOutcomeReceiver<Void?, CallException>()
    callControls[cookie]?.disconnect(
      DisconnectCause(DisconnectCause.LOCAL),
      context.mainExecutor,
      deferred,
    ) ?: throw IllegalArgumentException("Call with $cookie doesn't exist!")

    try {
      runBlocking { withTimeout(CALL_CONTROL_TIMEOUT) { deferred.await() } }
    } catch (e: TimeoutCancellationException) {
      throw RuntimeException("Disconnect call timeout after $CALL_CONTROL_TIMEOUT", e)
    }
  }

  @Rpc(description = "Get active call endpoint")
  fun requestCallEndpointSwitch(
    cookie: String,
    endpointType: Int? = null,
    endpointIdentifier: String? = null,
  ) {
    val endpointIdentifierUuid = endpointIdentifier?.let { ParcelUuid.fromString(it) }
    fun matchEndpoint(ep: CallEndpoint): Boolean {
      return (endpointIdentifierUuid == null || ep.identifier == endpointIdentifierUuid) &&
        (endpointType == null || ep.endpointType == endpointType)
    }
    val endpoint = runBlocking {
      withTimeoutOrNull(CALL_CONTROL_TIMEOUT) {
          availableCallEndpoints.first { it.any { ep -> matchEndpoint(ep) } }
        }
        ?.firstOrNull { matchEndpoint(it) } ?: throw RuntimeException("No available call endpoints")
    }

    val deferred = DeferredOutcomeReceiver<Void?, CallException>()
    callControls[cookie]?.requestCallEndpointChange(endpoint, context.mainExecutor, deferred)
      ?: throw IllegalArgumentException("Call with $cookie doesn't exist!")
    try {
      runBlocking { withTimeout(CALL_CONTROL_TIMEOUT) { deferred.await() } }
    } catch (e: TimeoutCancellationException) {
      throw RuntimeException("Request call endpoint switch timeout after $CALL_CONTROL_TIMEOUT", e)
    }
  }

  @Rpc(description = "Get available call endpoints")
  fun getAvailableCallEndpoints(): List<CallEndpoint> = availableCallEndpoints.value

  /** Gets all registered calls. */
  @Rpc(description = "Get all calls") fun getCalls(): List<Call> = inCallService.calls

  /** Clears all contacts. */
  @Rpc(description = "Clear all contacts")
  fun clearContacts() {
    val cursor =
      context.contentResolver.query(ContactsContract.Contacts.CONTENT_URI, null, null, null, null)
        ?: throw RuntimeException("Failed to query ${ContactsContract.Contacts.CONTENT_URI}")
    var deletedLines = 0
    cursor.use {
      while (it.moveToNext()) {
        val lookupKey = it.getString(it.getColumnIndex(ContactsContract.Contacts.LOOKUP_KEY))
        val uri = Uri.withAppendedPath(ContactsContract.Contacts.CONTENT_LOOKUP_URI, lookupKey)
        deletedLines += context.contentResolver.delete(uri, null, null)
      }
    }
    Log.i(TAG, "$deletedLines lines deleted")
  }

  /** Gets name, number and phone type of all contacts. */
  @Rpc(description = "Get all contacts")
  fun getContacts(): List<Map<String, Any>> {
    val cursor =
      context.contentResolver.query(
        ContactsContract.Data.CONTENT_URI,
        arrayOf(
          ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME,
          ContactsContract.CommonDataKinds.Phone.NUMBER,
          ContactsContract.CommonDataKinds.Phone.TYPE,
        ),
        null,
        null,
        null,
        null,
      ) ?: throw RuntimeException("Failed to query ${ContactsContract.Data.CONTENT_URI}")
    val result = mutableListOf<Map<String, Any>>()
    cursor.use {
      while (it.moveToNext()) {
        result.add(
          mapOf(
            SnippetConstants.CONTACT_NAME to it.getString(0),
            SnippetConstants.CONTACT_NUMBER to it.getString(1),
            SnippetConstants.CONTACT_PHONE_TYPE to it.getInt(2),
          )
        )
      }
      return result
    }
  }

  /** Adds contacts described by [contacts]. */
  @Rpc(description = "Add contacts")
  fun addContacts(contacts: JSONArray) {
    val ops = arrayListOf<ContentProviderOperation>()
    for (i in 0 until contacts.length()) {
      val backrefIndex = ops.size
      ops.add(
        ContentProviderOperation.newInsert(ContactsContract.RawContacts.CONTENT_URI)
          .withValue(ContactsContract.RawContacts.ACCOUNT_TYPE, null)
          .withValue(ContactsContract.RawContacts.ACCOUNT_NAME, null)
          .build()
      )
      val contact = contacts.getJSONObject(i)
      for (section in CONTACT_SECTIONS) {
        val op =
          ContentProviderOperation.newInsert(ContactsContract.Data.CONTENT_URI)
            .withValueBackReference(ContactsContract.Data.RAW_CONTACT_ID, backrefIndex)
            .withValue(ContactsContract.Data.MIMETYPE, section.mimeType)
        for ((fieldName, jsonField) in section.fields) {
          op.withValue(fieldName, contact.opt(jsonField))
        }
        ops.add(op.build())
      }
    }
    context.contentResolver.applyBatch(ContactsContract.AUTHORITY, ops)
  }

  /** Add call logs described in [logs]. */
  @Rpc(description = "Add call logs")
  fun addCallLogs(logs: JSONArray) {
    for (i in 0 until logs.length()) {
      val log = logs.getJSONObject(i)
      val contentValues =
        ContentValues().apply {
          put(CallLog.Calls.NUMBER, log.getString(SnippetConstants.CONTACT_NUMBER))
          put(CallLog.Calls.DATE, log.getLong(SnippetConstants.CALL_DATE))
          put(CallLog.Calls.DURATION, log.getLong(SnippetConstants.CALL_DURATION))
          put(CallLog.Calls.TYPE, log.getInt(SnippetConstants.CALL_TYPE))
          put(CallLog.Calls.CACHED_NAME, log.getString(SnippetConstants.CONTACT_NAME))
          put(CallLog.Calls.NEW, 1)
        }
      context.contentResolver.insert(CallLog.Calls.CONTENT_URI, contentValues)
    }
  }

  /** Gets all call logs. */
  @Rpc(description = "Get call logs")
  fun getCallLogs(): List<Map<String, Any>> {
    val cursor =
      context.contentResolver.query(
        CallLog.Calls.CONTENT_URI,
        arrayOf(
          CallLog.Calls.NUMBER,
          CallLog.Calls.DATE,
          CallLog.Calls.DURATION,
          CallLog.Calls.TYPE,
        ),
        null,
        null,
        null,
        null,
      ) ?: throw RuntimeException("Failed to query ${CallLog.Calls.CONTENT_URI}")
    val result = mutableListOf<Map<String, Any>>()
    cursor.use {
      while (cursor.moveToNext()) {
        result.add(
          mapOf(
            SnippetConstants.CONTACT_NUMBER to cursor.getString(0),
            SnippetConstants.CALL_DATE to cursor.getString(1),
            SnippetConstants.CALL_DURATION to cursor.getString(2),
            SnippetConstants.CALL_TYPE to cursor.getString(3),
          )
        )
      }
      return result
    }
  }

  /** Clears all call logs. */
  @Rpc(description = "Clear call logs")
  fun clearCallLogs() {
    context.contentResolver.delete(CallLog.Calls.CONTENT_URI, null, null)
  }

  /** Notifies all observers that MMS SMS db is changed. */
  @Rpc(description = "Notifies all observers that MMS SMS db is changed")
  fun notifyMmsSmsChange() {
    context.contentResolver.notifyChange(Telephony.MmsSms.CONTENT_URI, null)
  }

  @Rpc(description = "Disconnect all active calls.")
  fun disconnectAllCalls() {
    for (call in inCallService.calls) {
      Log.i(TAG, "Disconnecting call: ${call.details.handle}")
      call.disconnect()
    }
  }

  /** Enables or disables a phone account using TelecomManager directly. */
  @Rpc(description = "Enable or disable a phone account")
  fun setPhoneAccountEnabled(
    packageName: String,
    className: String,
    accountId: String,
    enabled: Boolean,
  ) {
    val handle = PhoneAccountHandle(ComponentName(packageName, className), accountId)
    telecomManager.enablePhoneAccount(handle, enabled)
  }

  /** Sets or clears user selected outgoing phone account using TelecomManager directly. */
  @Rpc(description = "Set user selected outgoing phone account")
  fun setUserSelectedOutgoingPhoneAccount(
    @RpcOptional packageName: String? = null,
    @RpcOptional className: String? = null,
    @RpcOptional accountId: String? = null,
  ) {
    telecomManager.userSelectedOutgoingPhoneAccount =
      if (packageName != null && className != null && accountId != null) {
        PhoneAccountHandle(ComponentName(packageName, className), accountId)
      } else {
        null
      }
  }

  /** Registers phone account for ConnectionService. */
  @Rpc(description = "Register phone account for a ConnectionService")
  fun registerPhoneAccount(
    packageName: String,
    className: String,
    accountId: String,
    accountLabel: String,
  ) {
    val phoneAccountHandle = PhoneAccountHandle(ComponentName(packageName, className), accountId)
    val phoneAccount =
      PhoneAccount.builder(phoneAccountHandle, accountLabel)
        .setCapabilities(PhoneAccount.CAPABILITY_CALL_PROVIDER)
        .addSupportedUriScheme(PhoneAccount.SCHEME_TEL)
        .build()
    telecomManager.registerPhoneAccount(phoneAccount)
  }

  /** Unregisters phone account for ConnectionService. */
  @Rpc(description = "Unregister phone account for a ConnectionService")
  fun unregisterPhoneAccount(packageName: String, className: String, accountId: String) {
    val phoneAccountHandle = PhoneAccountHandle(ComponentName(packageName, className), accountId)
    telecomManager.unregisterPhoneAccount(phoneAccountHandle)
  }

  private fun broadcastCallStateChangedEvent(name: String, handle: String, state: Int) {
    for (callbackId in inCallService.callbacks.keys) {
      postSnippetEvent(callbackId, SnippetConstants.CALL_STATE_CHANGED) {
        putString(SnippetConstants.FIELD_NAME, name)
        putString(SnippetConstants.FIELD_HANDLE, handle)
        putInt(SnippetConstants.FIELD_STATE, state)
      }
    }
  }

  private companion object {
    const val TAG = "TelecomSnippet"
    val CALL_CONTROL_TIMEOUT = 5.seconds

    class DeferredOutcomeReceiver<T, E : Exception>() : OutcomeReceiver<T, E> {
      val deferred = CompletableDeferred<T>()

      override fun onResult(result: T) {
        deferred.complete(result)
      }

      override fun onError(error: E) {
        deferred.completeExceptionally(error)
      }

      suspend fun await() = deferred.await()
    }

    val PHONE_ACCOUNT_HANDLE =
      PhoneAccountHandle(
        ComponentName(
          "com.google.wireless.android.pixel.bluetooth.snippet",
          "com.google.wireless.android.pixel.bluetooth.snippet.TelecomSnippet",
        ),
        "TelecomSnippet",
      )

    val PHONE_ACCOUNT =
      PhoneAccount.builder(PHONE_ACCOUNT_HANDLE, "Snippet Phone Account")
        .setCapabilities(
          PhoneAccount.CAPABILITY_SUPPORTS_TRANSACTIONAL_OPERATIONS or
            PhoneAccount.CAPABILITY_SELF_MANAGED
        )
        .build()

    data class ContactInsertSection(val mimeType: String, val fields: Map<String, String>)

    val CONTACT_SECTIONS =
      listOf(
        ContactInsertSection(
          mimeType = ContactsContract.CommonDataKinds.StructuredName.CONTENT_ITEM_TYPE,
          fields =
            mapOf(
              ContactsContract.CommonDataKinds.StructuredName.DISPLAY_NAME to
                SnippetConstants.CONTACT_NAME
            ),
        ),
        ContactInsertSection(
          mimeType = ContactsContract.CommonDataKinds.Phone.CONTENT_ITEM_TYPE,
          fields =
            mapOf(
              ContactsContract.CommonDataKinds.Phone.NUMBER to SnippetConstants.CONTACT_NUMBER,
              ContactsContract.CommonDataKinds.Phone.TYPE to SnippetConstants.CONTACT_PHONE_TYPE,
            ),
        ),
        ContactInsertSection(
          mimeType = ContactsContract.CommonDataKinds.Email.CONTENT_ITEM_TYPE,
          fields =
            mapOf(
              ContactsContract.CommonDataKinds.Email.ADDRESS to SnippetConstants.CONTACT_EMAIL,
              ContactsContract.CommonDataKinds.Email.TYPE to SnippetConstants.CONTACT_EMAIL_TYPE,
            ),
        ),
        ContactInsertSection(
          mimeType = ContactsContract.CommonDataKinds.Organization.CONTENT_ITEM_TYPE,
          fields =
            mapOf(
              ContactsContract.CommonDataKinds.Organization.COMPANY to
                SnippetConstants.CONTACT_COMPANY,
              ContactsContract.CommonDataKinds.Organization.JOB_DESCRIPTION to
                SnippetConstants.CONTACT_JOB_TITLE,
            ),
        ),
      )
  }
}
