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
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothGattService
import android.bluetooth.BluetoothManager
import android.util.Base64
import android.util.Log
import androidx.test.platform.app.InstrumentationRegistry
import com.google.android.mobly.snippet.Snippet
import com.google.android.mobly.snippet.event.EventCache
import com.google.android.mobly.snippet.event.SnippetEvent
import com.google.android.mobly.snippet.rpc.AsyncRpc
import com.google.android.mobly.snippet.rpc.Rpc
import com.google.android.mobly.snippet.rpc.RpcDefault
import com.google.wireless.android.pixel.bluetooth.snippet.Utils.postSnippetEvent
import java.util.UUID
import java.util.concurrent.Semaphore

@SuppressLint("MissingPermission")
class BluetoothGattClientSnippet : Snippet {

  inner class DefaultGattCallback(private val callbackId: String) : BluetoothGattCallback() {
    // TODO(b/317509809): Cover more callbacks
    override fun onConnectionStateChange(gatt: BluetoothGatt, status: Int, newState: Int) {
      Log.i(
        TAG,
        "onConnectionStateChange address=${gatt.device?.address}, status=$status, state=$newState",
      )
      postSnippetEvent(callbackId, SnippetConstants.GATT_CONNECTION_STATE_CHANGE) {
        putString(SnippetConstants.FIELD_DEVICE, gatt.device.address)
        putInt(SnippetConstants.FIELD_STATE, newState)
        putInt(SnippetConstants.FIELD_STATUS, status)
      }
    }

    override fun onServicesDiscovered(gatt: BluetoothGatt, status: Int) {
      Log.i(TAG, "onServicesDiscovered address=${gatt.device?.address}, status=$status")
      postSnippetEvent(callbackId, SnippetConstants.GATT_SERVICE_DISCOVERED) {
        putString(SnippetConstants.FIELD_DEVICE, gatt.device.address)
        putInt(SnippetConstants.FIELD_STATUS, status)
      }
    }

    override fun onServiceChanged(gatt: BluetoothGatt) {
      Log.i(TAG, "onServiceChanged address=${gatt.device.address}")
      postSnippetEvent(callbackId, SnippetConstants.GATT_SERVICE_CHANGED) {
        putString(SnippetConstants.FIELD_DEVICE, gatt.device.address)
      }
    }

    override fun onCharacteristicRead(
      gatt: BluetoothGatt,
      characteristic: BluetoothGattCharacteristic,
      value: ByteArray,
      status: Int,
    ) {
      Log.i(
        TAG,
        "onCharacteristicRead address=${gatt.device.address}, characteristic=${characteristic.uuid}, value=$value",
      )
      EventCache.getInstance()
        .postEvent(
          SnippetEvent(callbackId, SnippetConstants.GATT_CHARACTERISTIC_READ).apply {
            data.putString(SnippetConstants.FIELD_DEVICE, gatt.device.address)
            data.putInt(SnippetConstants.FIELD_HANDLE, characteristic.instanceId)
            data.putInt(SnippetConstants.FIELD_STATUS, status)
            data.putByteArray(SnippetConstants.FIELD_VALUE, value)
          }
        )
    }

    override fun onCharacteristicWrite(
      gatt: BluetoothGatt,
      characteristic: BluetoothGattCharacteristic,
      status: Int,
    ) {
      Log.i(
        TAG,
        "onCharacteristicWrite address=${gatt.device?.address}, characteristic=${characteristic.uuid}, status=$status",
      )
      postSnippetEvent(callbackId, SnippetConstants.GATT_CHARACTERISTIC_WRITE) {
        putString(SnippetConstants.FIELD_DEVICE, gatt.device.address)
        putInt(SnippetConstants.FIELD_HANDLE, characteristic.instanceId)
        putInt(SnippetConstants.FIELD_STATUS, status)
      }
      writeSemaphores.getOrPut(gatt) { Semaphore(0) }.release()
    }

    override fun onCharacteristicChanged(
      gatt: BluetoothGatt,
      characteristic: BluetoothGattCharacteristic,
      value: ByteArray,
    ) {
      postSnippetEvent(callbackId, SnippetConstants.GATT_CHARACTERISTIC_CHANGED) {
        putString(SnippetConstants.FIELD_DEVICE, gatt.device.address)
        putInt(SnippetConstants.FIELD_HANDLE, characteristic.instanceId)
        putByteArray(SnippetConstants.FIELD_VALUE, value)
      }
    }

    override fun onDescriptorRead(
      gatt: BluetoothGatt,
      descriptor: BluetoothGattDescriptor,
      status: Int,
      value: ByteArray,
    ) {
      Log.i(
        TAG,
        "onDescriptorRead address=${gatt.device.address}, descriptor=${descriptor.uuid}, status=$status",
      )
      postSnippetEvent(callbackId, SnippetConstants.GATT_DESCRIPTOR_READ) {
        putString(SnippetConstants.FIELD_DEVICE, gatt.device.address)
        putInt(SnippetConstants.FIELD_HANDLE, descriptor.characteristic.instanceId)
        putString(SnippetConstants.FIELD_UUID, descriptor.uuid.toString())
        putInt(SnippetConstants.FIELD_STATUS, status)
        putByteArray(SnippetConstants.FIELD_VALUE, value)
      }
    }

    override fun onDescriptorWrite(
      gatt: BluetoothGatt,
      descriptor: BluetoothGattDescriptor,
      status: Int,
    ) {
      postSnippetEvent(callbackId, SnippetConstants.GATT_DESCRIPTOR_WRITE) {
        putString(SnippetConstants.FIELD_DEVICE, gatt.device.address)
        putInt(SnippetConstants.FIELD_HANDLE, descriptor.characteristic.instanceId)
        putString(SnippetConstants.FIELD_UUID, descriptor.uuid.toString())
        putInt(SnippetConstants.FIELD_STATUS, status)
      }
    }

    override fun onPhyUpdate(gatt: BluetoothGatt, txPhy: Int, rxPhy: Int, status: Int) {
      postSnippetEvent(callbackId, SnippetConstants.GATT_PHY_UPDATE) {
        putString(SnippetConstants.FIELD_DEVICE, gatt.device.address)
        putInt(SnippetConstants.FIELD_TX_PHY, txPhy)
        putInt(SnippetConstants.FIELD_RX_PHY, rxPhy)
        putInt(SnippetConstants.FIELD_STATUS, status)
      }
    }

    override fun onMtuChanged(gatt: BluetoothGatt, mtu: Int, status: Int) {
      postSnippetEvent(callbackId, SnippetConstants.GATT_MTU_CHANGED) {
        putString(SnippetConstants.FIELD_DEVICE, gatt.device.address)
        putInt(SnippetConstants.FIELD_MTU, mtu)
        putInt(SnippetConstants.FIELD_STATUS, status)
      }
    }

    override fun onSubrateChange(gatt: BluetoothGatt, subrateMode: Int, status: Int) {
      postSnippetEvent(callbackId, SnippetConstants.GATT_SUBRATE_CHANGED) {
        putString(SnippetConstants.FIELD_DEVICE, gatt.device.address)
        putInt(SnippetConstants.GATT_FIELD_SUBRATE_MODE, subrateMode)
        putInt(SnippetConstants.FIELD_STATUS, status)
      }
    }

    override fun onReliableWriteCompleted(gatt: BluetoothGatt, status: Int) {
      Log.i(TAG, "onReliableWriteCompleted address=${gatt.device.address}, status=$status")
      postSnippetEvent(callbackId, SnippetConstants.GATT_RELIABLE_WRITE_COMPLETED) {
        putString(SnippetConstants.FIELD_DEVICE, gatt.device.address)
        putInt(SnippetConstants.FIELD_STATUS, status)
      }
    }

    override fun onConnectionUpdated(
      gatt: BluetoothGatt,
      interval: Int,
      latency: Int,
      supervisionTimeout: Int,
      status: Int,
    ) {
      postSnippetEvent(callbackId, SnippetConstants.GATT_CONNECTION_UPDATED) {
        putString(SnippetConstants.FIELD_DEVICE, gatt.device.address)
        putInt(SnippetConstants.GATT_FIELD_INTERVAL, interval)
        putInt(SnippetConstants.GATT_FIELD_LATENCY, latency)
        putInt(SnippetConstants.GATT_FIELD_SUPERVISION_TIMEOUT, supervisionTimeout)
        putInt(SnippetConstants.FIELD_STATUS, status)
      }
    }
  }

  private val instrumentation = InstrumentationRegistry.getInstrumentation()
  private val context = instrumentation.targetContext
  private val bluetoothManager = context.getSystemService(BluetoothManager::class.java)
  private val bluetoothAdapter = bluetoothManager.adapter
  // BluetoothGatt rejects any request when it is still processing previous request. Thus, we need
  // to wait until callbacks are received to send further requests.
  private val writeSemaphores = mutableMapOf<BluetoothGatt, Semaphore>()
  internal val gattClients = mutableMapOf<String, BluetoothGatt>()

  init {
    instrumentation.uiAutomation.adoptShellPermissionIdentity()
  }

  /**
   * Connects a GATT client instance to device [address] over [transport], and redirects callbacks
   * to the event queue with [callbackId].
   *
   * If request is issued on [BluetoothDevice.TRANSPORT_LE] transport, remote address type is
   * determined by [addressType] or using default value of [BluetoothDevice.ADDRESS_TYPE_RANDOM].
   */
  @AsyncRpc(description = "Connect a GATT client")
  fun gattConnect(
    callbackId: String,
    address: String,
    transport: Int,
    @RpcDefault(
      BluetoothDevice.ADDRESS_TYPE_RANDOM.toString(),
      converter = Utils.IntConverter::class,
    )
    addressType: Int = BluetoothDevice.ADDRESS_TYPE_RANDOM,
  ) {
    if (transport == BluetoothDevice.TRANSPORT_LE) {
        bluetoothAdapter.getRemoteLeDevice(address, addressType)
      } else {
        bluetoothAdapter.getRemoteDevice(address)
      }
      .connectGatt(context, false, DefaultGattCallback(callbackId), transport)
      .also { gattClients[callbackId] = it }
  }

  /**
   * Reconnects a GATT client instance of [cookie], and returns true if the connection attempt was
   * initiated successfully.
   */
  @Rpc(description = "Reconnects a GATT connection")
  fun gattReconnect(cookie: String): Boolean {
    return gattClients[cookie]?.connect()
      ?: throw IllegalArgumentException("GATT client $cookie not found")
  }

  /** Disconnects a GATT client instance of [cookie]. */
  @Rpc(description = "Disconnect a GATT connection")
  fun gattDisconnect(cookie: String) {
    gattClients[cookie]?.disconnect()
      ?: throw IllegalArgumentException("GATT client $cookie not found")
  }

  /** Closes a GATT client instance of [cookie]. */
  @Rpc(description = "Close a GATT client instance")
  fun gattClose(cookie: String) {
    gattClients.remove(cookie)?.close()
      ?: throw IllegalArgumentException("GATT client $cookie not found")
  }

  /** Discovers services on GATT client of [cookie]. */
  @Rpc(description = "Discovers services on GATT client of [cookie].")
  fun gattDiscoverServices(cookie: String): Boolean =
    gattClients[cookie]?.discoverServices()
      ?: throw IllegalArgumentException("Client $cookie doesn't exist!")

  /** Gets services on GATT client of [cookie]. */
  @Rpc(description = "Get services on GATT client of [cookie].")
  fun gattGetServices(cookie: String): List<BluetoothGattService> =
    gattClients[cookie]?.services ?: throw IllegalArgumentException("Client $cookie doesn't exist!")

  /**
   * Reads from characteristic [characteristicHandle] on GATT client of [cookie], and returns true
   * if success, false if fail.
   */
  @Rpc(description = "Reads a characteristic value")
  fun gattReadCharacteristic(cookie: String, characteristicHandle: Int): Boolean {
    val client =
      gattClients[cookie] ?: throw IllegalArgumentException("Client $cookie doesn't exist!")
    val characteristic =
      client.findCharacteristicByHandle(characteristicHandle)
        ?: throw IllegalArgumentException("Invalid characteristic handle $characteristicHandle")
    return client.readCharacteristic(characteristic)
  }

  /**
   * Writes [value] with method [writeType] to characteristic [characteristicHandle] on GATT client
   * of [cookie], and returns the status code.
   */
  @Rpc(description = "Writes a characteristic value")
  fun gattWriteCharacteristic(
    cookie: String,
    characteristicHandle: Int,
    value: ByteArray,
    writeType: Int,
  ): Int {
    val client =
      gattClients[cookie] ?: throw IllegalArgumentException("Client $cookie doesn't exist!")
    writeSemaphores.getOrPut(client) { Semaphore(1) }.acquire()
    val characteristic =
      client.findCharacteristicByHandle(characteristicHandle)
        ?: throw IllegalArgumentException("Invalid characteristic handle $characteristicHandle")
    val result = client.writeCharacteristic(characteristic, value, writeType)
    if (result != BluetoothGatt.GATT_SUCCESS) {
      writeSemaphores.getOrPut(client) { Semaphore(1) }.release()
    }
    return result
  }

  /**
   * Continuously writes [value] in segmentations to characteristic [characteristicHandle] on GATT
   * client of [cookie] until all bytes are sent.
   */
  @Rpc(description = "Writes a characteristic with long value")
  fun gattWriteCharacteristicLong(
    cookie: String,
    characteristicHandle: Int,
    value: String,
    mtu: Int,
    writeType: Int,
  ) {
    val client =
      gattClients[cookie] ?: throw IllegalArgumentException("Client $cookie doesn't exist!")
    val characteristic =
      client.findCharacteristicByHandle(characteristicHandle)
        ?: throw IllegalArgumentException("Invalid characteristic handle $characteristicHandle")
    val buf = Base64.decode(value, Base64.NO_WRAP)
    var byteWritten = 0
    while (byteWritten < buf.size) {
      writeSemaphores.getOrPut(client) { Semaphore(1) }.acquire()
      val result =
        client.writeCharacteristic(
          characteristic,
          buf.sliceArray(byteWritten until buf.size.coerceAtMost(byteWritten + mtu)),
          writeType,
        )
      if (result != BluetoothGatt.GATT_SUCCESS) {
        writeSemaphores.getOrPut(client) { Semaphore(1) }.release()
      } else {
        byteWritten += mtu
      }
    }
  }

  /**
   * Sets characteristic [characteristicHandle] on GATT client of [cookie] state to [enabled], and
   * returns true if success, false if fail.
   */
  @Rpc(description = "Subscribes a characteristic")
  fun gattSubscribeCharacteristic(
    cookie: String,
    characteristicHandle: Int,
    enabled: Boolean,
  ): Boolean {
    val client =
      gattClients[cookie] ?: throw IllegalArgumentException("Client $cookie doesn't exist!")
    val characteristic =
      client.findCharacteristicByHandle(characteristicHandle)
        ?: throw IllegalArgumentException("Invalid characteristic handle $characteristicHandle")
    return client.setCharacteristicNotification(characteristic, enabled)
  }

  /**
   * Reads [descriptorUuid] descriptor of characteristic [characteristicHandle] on GATT client of
   * [cookie], and returns true if the read operation was started successfully.
   */
  @Rpc(description = "Reads a characteristic descriptor")
  fun gattReadDescriptor(
    cookie: String,
    characteristicHandle: Int,
    descriptorUuid: String,
  ): Boolean {
    val client =
      gattClients[cookie] ?: throw IllegalArgumentException("Client $cookie doesn't exist!")
    val characteristic =
      client.findCharacteristicByHandle(characteristicHandle)
        ?: throw IllegalArgumentException("Invalid characteristic handle $characteristicHandle")
    val descriptor =
      characteristic.getDescriptor(UUID.fromString(descriptorUuid))
        ?: throw IllegalArgumentException("Invalid descriptor UUID $descriptorUuid")
    return client.readDescriptor(descriptor)
  }

  /**
   * Writes [value] to [descriptorUuid] descriptor of characteristic in [characteristicHandle] on
   * GATT client of [cookie], and returns the status code.
   */
  @Rpc(description = "Writes a characteristic descriptor")
  fun gattWriteDescriptor(
    cookie: String,
    characteristicHandle: Int,
    descriptorUuid: String,
    value: ByteArray,
  ): Int {
    val client =
      gattClients[cookie] ?: throw IllegalArgumentException("Client $cookie doesn't exist!")
    val characteristic =
      client.findCharacteristicByHandle(characteristicHandle)
        ?: throw IllegalArgumentException("Invalid characteristic handle $characteristicHandle")
    val descriptor =
      characteristic.getDescriptor(UUID.fromString(descriptorUuid))
        ?: throw IllegalArgumentException("Invalid descriptor UUID $descriptorUuid")
    return client.writeDescriptor(descriptor, value)
  }

  /** Set preferred PHY of GATT connection [cookie] to [txPhy], [rxPhy], [phyOptions]. */
  @Rpc(description = "Set Preferred phy of client")
  fun gattSetPreferredPhy(cookie: String, txPhy: Int, rxPhy: Int, phyOptions: Int) {
    val client =
      gattClients[cookie] ?: throw IllegalArgumentException("Client $cookie doesn't exist!")
    client.setPreferredPhy(txPhy, rxPhy, phyOptions)
  }

  /**
   * Request MTU of GATT connection [cookie] to [mtu], and returns true if success, false otherwise.
   */
  @Rpc(description = "Requests MTU of client")
  fun gattRequestMtu(cookie: String, mtu: Int): Boolean {
    val client =
      gattClients[cookie] ?: throw IllegalArgumentException("Client $cookie doesn't exist!")
    return client.requestMtu(mtu)
  }

  /**
   * Requests connection parameters of GATT connection [cookie] to [connectionPriority], and returns
   * true if success, false otherwise.
   */
  @Rpc(description = "Request connection priority")
  fun gattRequestConnectionPriority(cookie: String, connectionPriority: Int): Boolean {
    val client =
      gattClients[cookie] ?: throw IllegalArgumentException("Client $cookie doesn't exist!")
    return client.requestConnectionPriority(connectionPriority)
  }

  /** Requests subrate mode of GATT connection [cookie] to [mode], and returns the status code. */
  @Rpc(description = "Request subrate mode")
  fun gattRequestSubrateMode(cookie: String, mode: Int): Int {
    val client =
      gattClients[cookie] ?: throw IllegalArgumentException("Client $cookie doesn't exist!")
    return client.requestSubrateMode(mode)
  }

  /** Begins a reliable write transaction for [cookie]. */
  @Rpc(description = "Begins a reliable write transaction")
  fun gattBeginReliableWrite(cookie: String): Boolean {
    val client =
      gattClients[cookie] ?: throw IllegalArgumentException("Client $cookie doesn't exist!")
    return client.beginReliableWrite()
  }

  /** Executes the reliable write transaction for [cookie]. */
  @Rpc(description = "Executes the reliable write transaction")
  fun gattExecuteReliableWrite(cookie: String): Boolean {
    val client =
      gattClients[cookie] ?: throw IllegalArgumentException("Client $cookie doesn't exist!")
    return client.executeReliableWrite()
  }

  /** Aborts the reliable write transaction for [cookie]. */
  @Rpc(description = "Aborts the reliable write transaction")
  fun gattAbortReliableWrite(cookie: String) {
    val client =
      gattClients[cookie] ?: throw IllegalArgumentException("Client $cookie doesn't exist!")
    client.abortReliableWrite()
  }

  companion object {
    const val TAG = "BluetoothGattClientSnippet"

    /**
     * Finds a characteristic by [characteristicHandle] among all services. If such characteristic
     * doesn't exist, return null.
     *
     * GATT spec promises that all handles are unique on the same server.
     */
    fun BluetoothGatt.findCharacteristicByHandle(
      characteristicHandle: Int
    ): BluetoothGattCharacteristic? =
      this.services
        .asSequence()
        .flatMap { it.characteristics }
        .firstOrNull { it.instanceId == characteristicHandle }
  }
}
