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

import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothServerSocket
import android.bluetooth.BluetoothSocket
import android.bluetooth.BluetoothSocketSettings
import android.util.Base64
import android.util.Log
import androidx.annotation.VisibleForTesting
import androidx.test.platform.app.InstrumentationRegistry
import com.google.android.mobly.snippet.Snippet
import com.google.android.mobly.snippet.rpc.Rpc
import com.google.android.mobly.snippet.rpc.RpcDefault
import com.google.android.mobly.snippet.rpc.RpcOptional
import java.io.IOException
import java.util.UUID
import java.util.concurrent.ExecutionException
import java.util.concurrent.Executors
import java.util.concurrent.Future
import java.util.concurrent.TimeUnit

class BluetoothSocketSnippet : Snippet {
  private val instrumentation = InstrumentationRegistry.getInstrumentation()
  private val context = instrumentation.targetContext
  private val bluetoothAdapter = context.getSystemService(BluetoothManager::class.java).adapter
  private val servers = mutableMapOf<String, BluetoothServerSocket>()
  private val sockets = mutableMapOf<String, BluetoothSocket>()
  private val threadPool = Executors.newCachedThreadPool()
  private val connectingFutures = mutableMapOf<String, Future<*>>()

  init {
    instrumentation.uiAutomation.adoptShellPermissionIdentity()
  }

  // --- L2CAP RPCs ---

  @Rpc(description = "Connect an L2CAP channel")
  fun l2capConnect(
    address: String,
    secure: Boolean,
    psm: Int,
    @RpcDefault(
      BluetoothDevice.ADDRESS_TYPE_RANDOM.toString(),
      converter = Utils.IntConverter::class,
    )
    addressType: Int = BluetoothDevice.ADDRESS_TYPE_RANDOM,
    @RpcDefault(BluetoothDevice.TRANSPORT_LE.toString(), converter = Utils.IntConverter::class)
    transport: Int = BluetoothDevice.TRANSPORT_LE,
    @RpcDefault(true.toString(), converter = Utils.BooleanConverter::class) blocking: Boolean = true,
  ): String {
    val socket =
      when (transport) {
        BluetoothDevice.TRANSPORT_LE,
        BluetoothDevice.TRANSPORT_AUTO -> {
          val device = bluetoothAdapter.getRemoteLeDevice(address, addressType)
          when (secure) {
            true -> device.createL2capChannel(psm)
            false -> device.createInsecureL2capChannel(psm)
          }
        }
        BluetoothDevice.TRANSPORT_BREDR -> {
          val device = bluetoothAdapter.getRemoteDevice(address)
          when (secure) {
            true ->
              device.javaClass
                .getMethod("createL2capSocket", java.lang.Integer.TYPE)
                .invoke(device, psm) as BluetoothSocket
            false ->
              device.javaClass
                .getMethod("createInsecureL2capSocket", java.lang.Integer.TYPE)
                .invoke(device, psm) as BluetoothSocket
          }
        }
        else -> throw IllegalArgumentException("Unsupported transport: $transport")
      }
    val cookie = UUID.randomUUID().toString()
    sockets[cookie] = socket

    if (blocking) {
      socket.connect()
    } else {
      connectingFutures[cookie] = threadPool.submit {
        try {
          socket.connect()
        } catch (e: java.io.IOException) {
          Log.e(TAG, "Failed to connect to L2CAP channel", e)
          throw e
        }
      }
    }
    return cookie
  }

  @Rpc(description = "Open an L2CAP server")
  fun l2capOpenServer(
    secure: Boolean,
    psm: Int,
    @RpcDefault(BluetoothDevice.TRANSPORT_LE.toString(), converter = Utils.IntConverter::class)
    transport: Int = BluetoothDevice.TRANSPORT_LE,
  ): List<Any> {
    val serverSocket =
      when (transport) {
        BluetoothDevice.TRANSPORT_LE,
        BluetoothDevice.TRANSPORT_AUTO -> {
          when (secure) {
            true -> bluetoothAdapter.listenUsingL2capChannel()
            false -> bluetoothAdapter.listenUsingInsecureL2capChannel()
          }
        }
        BluetoothDevice.TRANSPORT_BREDR -> {
          val method =
            bluetoothAdapter.javaClass.getMethod(
              "listenUsingL2capOn",
              java.lang.Integer.TYPE,
              java.lang.Boolean.TYPE,
              java.lang.Boolean.TYPE,
            )
          method.invoke(bluetoothAdapter, psm, secure, secure) as BluetoothServerSocket
        }
        else -> throw IllegalArgumentException("Unsupported transport: $transport")
      }
    val cookie = UUID.randomUUID().toString()
    servers[cookie] = serverSocket
    return listOf(cookie, serverSocket.psm)
  }

  @Rpc(description = "Connect a socket using BluetoothSocketSettings")
  fun socketConnectWithSettings(
    address: String,
    @RpcDefault(
      BluetoothDevice.ADDRESS_TYPE_RANDOM.toString(),
      converter = Utils.IntConverter::class,
    )
    addressType: Int = BluetoothDevice.ADDRESS_TYPE_RANDOM,
    settings: BluetoothSocketSettings,
  ): String {
    // If it is RFCOMM, we use getRemoteDevice to avoid address type issues on older devices,
    // though createUsingSocketSettings is only on newer APIs.
    val device =
      if (settings.socketType == BluetoothSocket.TYPE_RFCOMM) {
        bluetoothAdapter.getRemoteDevice(address)
      } else {
        bluetoothAdapter.getRemoteLeDevice(address, addressType)
      }
    val socket = device.createUsingSocketSettings(settings)
    socket.connect()
    val cookie = UUID.randomUUID().toString()
    sockets[cookie] = socket
    return cookie
  }

  @Rpc(description = "Listen using BluetoothSocketSettings")
  fun socketListenWithSettings(settings: BluetoothSocketSettings): List<Any> {
    val serverSocket = bluetoothAdapter.listenUsingSocketSettings(settings)
    val cookie = UUID.randomUUID().toString()
    servers[cookie] = serverSocket
    return listOf(cookie, serverSocket.psm)
  }

  // --- RFCOMM RPCs ---

  @Rpc(description = "Connect to an RFCOMM channel")
  fun rfcommConnect(
    address: String,
    secure: Boolean,
    uuid: String,
    @RpcDefault(true.toString(), converter = Utils.BooleanConverter::class) blocking: Boolean = true,
  ): String {
    val device = bluetoothAdapter.getRemoteDevice(address)
    val socket =
      if (secure) {
        device.createRfcommSocketToServiceRecord(UUID.fromString(uuid))
      } else {
        device.createInsecureRfcommSocketToServiceRecord(UUID.fromString(uuid))
      }

    val cookie = UUID.randomUUID().toString()
    sockets[cookie] = socket

    if (blocking) {
      socket.connect()
    } else {
      connectingFutures[cookie] = threadPool.submit {
        try {
          socket.connect()
        } catch (e: java.io.IOException) {
          Log.e(TAG, "Failed to connect to RFCOMM channel", e)
          throw e
        }
      }
    }
    return cookie
  }

  @Rpc(description = "Wait for a socket connection to complete")
  fun socketWaitForConnectionComplete(
    cookie: String,
    @RpcDefault(
      DEFAULT_CONNECTION_TIMEOUT_MILLISECONDS.toString(),
      converter = Utils.LongConverter::class,
    )
    timeoutMilliseconds: Long = DEFAULT_CONNECTION_TIMEOUT_MILLISECONDS,
  ) {
    val future =
      connectingFutures[cookie] ?: throw IllegalArgumentException("No connection on cookie $cookie")
    try {
      future.get(timeoutMilliseconds, TimeUnit.MILLISECONDS)
    } catch (e: ExecutionException) {
      throw e.cause ?: e
    }
  }

  @Rpc(description = "Open an RFCOMM server")
  fun rfcommOpenServer(secure: Boolean, uuid: String): String {
    val serverSocket =
      if (secure) {
        bluetoothAdapter.listenUsingRfcommWithServiceRecord(uuid, UUID.fromString(uuid))
      } else {
        bluetoothAdapter.listenUsingInsecureRfcommWithServiceRecord(uuid, UUID.fromString(uuid))
      }
    val cookie = UUID.randomUUID().toString()
    servers[cookie] = serverSocket
    return cookie
  }

  // --- Unified RPCs ---

  @Rpc(description = "Wait for an incoming connection")
  fun socketAccept(serverCookie: String): String {
    val server =
      servers[serverCookie] ?: throw IllegalArgumentException("No server on cookie $serverCookie")
    val socket = server.accept(30000) ?: throw RuntimeException("Accept timeout")
    val cookie = UUID.randomUUID().toString()
    sockets[cookie] = socket
    return cookie
  }

  @Rpc(description = "Close a socket or server")
  fun socketClose(cookie: String) {
    if (cookie in sockets) {
      sockets.remove(cookie)?.close()
    } else if (cookie in servers) {
      servers.remove(cookie)?.close()
    }
    connectingFutures.remove(cookie)?.cancel(true)
  }

  @Rpc(description = "Read data from a socket")
  fun socketRead(cookie: String, @RpcOptional bytesToRead: Int?): String {
    val socket = sockets[cookie] ?: throw IllegalArgumentException("No socket on cookie $cookie")
    val inputStream = socket.inputStream
    var bytesRead = 0
    val defaultBufSize =
      if (socket.connectionType == BluetoothSocket.TYPE_L2CAP) {
        socket.maxReceivePacketSize.takeIf { it > 0 } ?: 65535
      } else {
        65535
      }
    val actualBuf = ByteArray(bytesToRead ?: defaultBufSize)

    if (bytesToRead == null) {
      val result = inputStream.read(actualBuf)
      if (result == -1) {
        throw IOException("-1")
      }
      bytesRead = result
    } else {
      while (bytesRead < bytesToRead) {
        val result = inputStream.read(actualBuf, bytesRead, bytesToRead - bytesRead)
        if (result == -1) {
          throw IOException("-1")
        }
        bytesRead += result
      }
    }
    return Base64.encodeToString(actualBuf, 0, bytesRead, Base64.NO_WRAP)
  }

  @Rpc(description = "Write data to a socket")
  fun socketWrite(cookie: String, data: String) {
    val socket = sockets[cookie] ?: throw IllegalArgumentException("No socket on cookie $cookie")
    socket.outputStream.write(Base64.decode(data, Base64.NO_WRAP))
  }

  @Rpc(description = "Check available bytes on a socket")
  fun socketAvailable(cookie: String): Int {
    val socket = sockets[cookie] ?: throw IllegalArgumentException("No socket on cookie $cookie")
    return socket.inputStream.available()
  }

  @Rpc(description = "Check if socket is connected")
  fun socketIsConnected(cookie: String): Boolean {
    return sockets[cookie]?.isConnected ?: false
  }

  @VisibleForTesting fun getServers() = this.servers

  @VisibleForTesting fun getSockets() = this.sockets

  private companion object {
    const val TAG = "BluetoothSocketSnippet"
    const val DEFAULT_CONNECTION_TIMEOUT_MILLISECONDS = 10_000L
  }
}
