// 同花顺 Android 交易模块字节边界采集脚本。
// 采集点：Java jniRequest 入参，以及 libweituo.so 内部派发函数入参。

'use strict';

const CONFIG = {
  javaClass: 'com.hexin.android.mastermodule.MasterModuleBridge',
  moduleName: 'libweituo.so',
  maxSampleBytes: 4096,
  nativeOffsets: [
    { name: 'native_jniRequest', offset: '0x45b2d0', kind: 'jni_byte_array', byteArrayArg: 3 },
    { name: 'native_request_e8e24', offset: '0xe8e24', kind: 'ptr_len', ptrArg: 0, lenArg: 1 },
    { name: 'native_request_e8718', offset: '0xe8718', kind: 'ptr_len', ptrArg: 0, lenArg: 1 },
  ],
};

function nowIso() {
  return new Date().toISOString();
}

function status(stage, ok, extra) {
  send(Object.assign({ type: 'hook_status', ts: nowIso(), stage: stage, ok: ok }, extra || {}));
}

function bytesToBase64(bytes) {
  const table = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
  let out = '';
  let i = 0;
  for (; i + 2 < bytes.length; i += 3) {
    const n = (bytes[i] << 16) | (bytes[i + 1] << 8) | bytes[i + 2];
    out += table[(n >> 18) & 63] + table[(n >> 12) & 63] + table[(n >> 6) & 63] + table[n & 63];
  }
  if (i < bytes.length) {
    let n = bytes[i] << 16;
    out += table[(n >> 18) & 63];
    if (i + 1 < bytes.length) {
      n |= bytes[i + 1] << 8;
      out += table[(n >> 12) & 63] + table[(n >> 6) & 63] + '=';
    } else {
      out += table[(n >> 12) & 63] + '==';
    }
  }
  return out;
}

function arrayBufferToBytes(buffer) {
  return Array.prototype.slice.call(new Uint8Array(buffer));
}

function emitBytes(source, hook, length, bytes, extra) {
  send(Object.assign({
    type: 'byte_sample',
    ts: nowIso(),
    source: source,
    hook: hook,
    length: length,
    sample_len: bytes.length,
    truncated: bytes.length < length,
    sample_base64: bytesToBase64(bytes),
  }, extra || {}));
}

function captureJavaArray(hook, byteArray) {
  if (byteArray === null || byteArray === undefined) {
    emitBytes('java', hook, 0, [], { null_value: true });
    return;
  }
  const length = byteArray.length;
  const sampleLen = Math.min(length, CONFIG.maxSampleBytes);
  const bytes = [];
  for (let i = 0; i < sampleLen; i += 1) {
    bytes.push(byteArray[i] & 0xff);
  }
  emitBytes('java', hook, length, bytes, { class_name: CONFIG.javaClass });
}

function captureJniByteArray(hook, jarrayHandle, argIndex) {
  Java.perform(function () {
    const env = Java.vm.getEnv();
    const length = env.getArrayLength(jarrayHandle);
    const sampleLen = Math.min(length, CONFIG.maxSampleBytes);
    const elements = env.getByteArrayElements(jarrayHandle, NULL);
    try {
      const buffer = Memory.readByteArray(elements, sampleLen);
      emitBytes('native', hook, length, arrayBufferToBytes(buffer), { arg_index: argIndex });
    } finally {
      env.releaseByteArrayElements(jarrayHandle, elements, 0);
    }
  });
}

function capturePointerBytes(hook, dataPtr, lenValue, ptrArg, lenArg) {
  if (dataPtr.isNull()) {
    emitBytes('native', hook, 0, [], { null_value: true, ptr_arg: ptrArg, len_arg: lenArg });
    return;
  }
  const length = lenValue.toUInt32();
  const sampleLen = Math.min(length, CONFIG.maxSampleBytes);
  const buffer = Memory.readByteArray(dataPtr, sampleLen);
  emitBytes('native', hook, length, arrayBufferToBytes(buffer), {
    ptr_arg: ptrArg,
    len_arg: lenArg,
    data_ptr: dataPtr.toString(),
  });
}

function hookJavaBridge() {
  Java.perform(function () {
    try {
      const Bridge = Java.use(CONFIG.javaClass);
      const overload = Bridge.jniRequest.overload('android.content.Context', '[B');
      overload.implementation = function (context, payload) {
        try {
          captureJavaArray('MasterModuleBridge.jniRequest', payload);
        } catch (err) {
          status('java_jniRequest_capture', false, { error: String(err), stack: String(err.stack || '') });
        }
        return overload.call(this, context, payload);
      };
      status('java_jniRequest_hook', true, { class_name: CONFIG.javaClass });
    } catch (err) {
      status('java_jniRequest_hook', false, { class_name: CONFIG.javaClass, error: String(err) });
    }
  });
}

function hookNativeOnce() {
  const module = Process.findModuleByName(CONFIG.moduleName);
  if (module === null) {
    return false;
  }
  CONFIG.nativeOffsets.forEach(function (item) {
    const address = module.base.add(ptr(item.offset));
    try {
      Interceptor.attach(address, {
        onEnter(args) {
          try {
            if (item.kind === 'jni_byte_array') {
              captureJniByteArray(item.name, args[item.byteArrayArg], item.byteArrayArg);
            } else if (item.kind === 'ptr_len') {
              capturePointerBytes(item.name, args[item.ptrArg], args[item.lenArg], item.ptrArg, item.lenArg);
            }
          } catch (err) {
            status(item.name + '_capture', false, { error: String(err), stack: String(err.stack || '') });
          }
        },
      });
      status('native_hook', true, { module_name: CONFIG.moduleName, hook: item.name, address: address.toString(), offset: item.offset });
    } catch (err) {
      status('native_hook', false, { module_name: CONFIG.moduleName, hook: item.name, offset: item.offset, error: String(err) });
    }
  });
  return true;
}

hookJavaBridge();

if (!hookNativeOnce()) {
  status('native_wait', true, { module_name: CONFIG.moduleName });
  const timer = setInterval(function () {
    if (hookNativeOnce()) {
      clearInterval(timer);
    }
  }, 500);
}