/*! Modified JASSUB 2.5.23 derivative. Mixed native/JavaScript licenses; not MIT-only.
 * Full copyright and license notices: ./LICENSES.txt
 * Complete local preferred source, build and relink information: ./SOURCE.json
 * Source archive: ./corresponding-source.tar.gz
 */
var __create = Object.create;
var __defProp = Object.defineProperty;
var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
var __getOwnPropNames = Object.getOwnPropertyNames;
var __getProtoOf = Object.getPrototypeOf;
var __hasOwnProp = Object.prototype.hasOwnProperty;
var __commonJS = (cb, mod) => function __require() {
  try {
    return mod || (0, cb[__getOwnPropNames(cb)[0]])((mod = { exports: {} }).exports, mod), mod.exports;
  } catch (e) {
    throw mod = 0, e;
  }
};
var __copyProps = (to, from, except, desc) => {
  if (from && typeof from === "object" || typeof from === "function") {
    for (let key of __getOwnPropNames(from))
      if (!__hasOwnProp.call(to, key) && key !== except)
        __defProp(to, key, { get: () => from[key], enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable });
  }
  return to;
};
var __toESM = (mod, isNodeMode, target) => (target = mod != null ? __create(__getProtoOf(mod)) : {}, __copyProps(
  // If the importer is in node compatibility mode or this is not an ESM
  // file that has been converted to a CommonJS file using a Babel-
  // compatible transform (i.e. "__esModule" has not been set), then set
  // "default" to the CommonJS "module.exports" for node compatibility.
  isNodeMode || !mod || !mod.__esModule ? __defProp(target, "default", { value: mod, enumerable: true }) : target,
  mod
));

// node_modules/throughput/index.js
var require_throughput = __commonJS({
  "node_modules/throughput/index.js"(exports, module) {
    var hrtime = typeof process !== "undefined" && !!process.hrtime;
    var maxTick = 65535;
    var resolution = 10;
    var timeDiff = hrtime ? 1e9 / resolution : 1e3 / resolution;
    var now = hrtime ? () => {
      const [seconds, nanoseconds] = process.hrtime();
      return seconds * 1e9 + nanoseconds;
    } : () => performance.now();
    function getTick(start) {
      return (now() - start) / timeDiff & maxTick;
    }
    module.exports = function(seconds) {
      const start = now();
      const size = resolution * (seconds || 5);
      const buffer2 = [0];
      let pointer = 1;
      let last = getTick(start) - 1 & maxTick;
      return function(delta) {
        const tick = getTick(start);
        let dist = tick - last & maxTick;
        if (dist > size) dist = size;
        last = tick;
        while (dist--) {
          if (pointer === size) pointer = 0;
          buffer2[pointer] = buffer2[pointer === 0 ? size - 1 : pointer - 1];
          pointer++;
        }
        if (delta) buffer2[pointer - 1] += delta;
        const top = buffer2[pointer - 1];
        const btm = buffer2.length < size ? 0 : buffer2[pointer === size ? 0 : pointer];
        return buffer2.length < resolution ? top : (top - btm) * resolution / buffer2.length;
      };
    };
  }
});

// node_modules/abslink/src/types.js
var WireValueType = {
  RAW: "RAW",
  PROXY: "PROXY",
  THROW: "THROW",
  HANDLER: "HANDLER"
};
var MessageType = {
  GET: "GET",
  SET: "SET",
  APPLY: "APPLY",
  CONSTRUCT: "CONSTRUCT",
  RELEASE: "RELEASE"
};

// node_modules/abslink/src/abslink.js
var proxyMarker = /* @__PURE__ */ Symbol("Abslink.proxy");
var releaseProxy = /* @__PURE__ */ Symbol("Abslink.releaseProxy");
var finalizer = /* @__PURE__ */ Symbol("Abslink.finalizer");
var throwMarker = /* @__PURE__ */ Symbol("Abslink.thrown");
var isObject = (val) => typeof val === "object" && val !== null || typeof val === "function";
var proxyTransferHandler = {
  canHandle: (val) => isObject(val) && proxyMarker in val,
  serialize(obj, ep) {
    const markerID = obj[proxyMarker];
    expose(obj, ep, markerID);
    return [markerID, []];
  },
  deserialize(markerID, ep) {
    return wrap(ep, markerID);
  }
};
var throwTransferHandler = {
  canHandle: (value) => isObject(value) && throwMarker in value,
  serialize({ value }) {
    let serialized;
    if (value instanceof Error) {
      serialized = {
        isError: true,
        value: {
          message: value.message,
          name: value.name,
          stack: value.stack
        }
      };
    } else {
      serialized = { isError: false, value };
    }
    return [serialized, []];
  },
  deserialize(serialized) {
    if (serialized.isError) {
      throw Object.assign(new Error(serialized.value.message), serialized.value);
    }
    throw serialized.value;
  }
};
var transferHandlers = /* @__PURE__ */ new Map([
  ["proxy", proxyTransferHandler],
  ["throw", throwTransferHandler]
]);
function filterPath(path, obj) {
  let parent = obj;
  const parentPath = path.slice(0, -1);
  for (const segment of parentPath) {
    if (Object.prototype.hasOwnProperty.call(parent, segment)) {
      parent = parent[segment];
    }
  }
  const lastSegment = path[path.length - 1];
  const RawValue = lastSegment ? parent[lastSegment] : parent;
  return { parent, RawValue, lastSegment };
}
function expose(obj, ep, rootMarkerID) {
  ep.on("message", function callback(data) {
    if (!data)
      return;
    const { id, type, path, markerID } = {
      path: [],
      ...data
    };
    if (markerID !== rootMarkerID)
      return;
    const argumentList = (data.argumentList ?? []).map((v) => fromWireValue(v, ep));
    let returnValue;
    try {
      const { parent, RawValue, lastSegment } = filterPath(path, obj);
      switch (type) {
        case MessageType.GET:
          returnValue = RawValue;
          break;
        case MessageType.SET:
          parent[lastSegment] = fromWireValue(data.value, ep);
          returnValue = true;
          break;
        case MessageType.APPLY:
          returnValue = RawValue.apply(parent, argumentList);
          break;
        case MessageType.CONSTRUCT:
          returnValue = new RawValue(...argumentList);
          break;
        case MessageType.RELEASE:
          returnValue = void 0;
          break;
        default:
          return;
      }
    } catch (value) {
      returnValue = { value, [throwMarker]: 0 };
    }
    Promise.resolve(returnValue).catch((value) => {
      return { value, [throwMarker]: 0 };
    }).then((returnValue2) => {
      if (type === MessageType.CONSTRUCT && !(isObject(returnValue2) && throwMarker in returnValue2)) {
        returnValue2 = proxy(returnValue2);
      }
      const [wireValue, transfer2] = toWireValue(returnValue2, ep);
      ep.postMessage({ ...wireValue, id, markerID: rootMarkerID }, transfer2);
      if (type === MessageType.RELEASE) {
        ep.off("message", callback);
        obj[finalizer]?.();
      }
    }).catch((_) => {
      const [wireValue, transfer2] = toWireValue({
        value: new TypeError("Unserializable return value"),
        [throwMarker]: 0
      }, ep);
      ep.postMessage({ ...wireValue, id, markerID: rootMarkerID }, transfer2);
    });
  });
  return obj;
}
function wrap(endpoint, rootMarkerID) {
  const pendingListeners = /* @__PURE__ */ new Map();
  endpoint.on("message", (data) => {
    if (!data?.id) {
      return;
    }
    const resolver = pendingListeners.get(data.id);
    if (!resolver) {
      return;
    }
    try {
      resolver(data);
    } finally {
      pendingListeners.delete(data.id);
    }
  });
  return createProxy({ endpoint, pendingListeners, rootMarkerID });
}
function throwIfProxyReleased(isReleased) {
  if (isReleased) {
    throw new Error("Proxy has been released and is not useable");
  }
}
async function releaseEndpoint(epWithPendingListeners) {
  await requestResponseMessage(epWithPendingListeners, { type: MessageType.RELEASE });
}
var proxyCounter = /* @__PURE__ */ new WeakMap();
var proxyFinalizers = "FinalizationRegistry" in globalThis && new FinalizationRegistry((epWithPendingListeners) => {
  const newCount = (proxyCounter.get(epWithPendingListeners) ?? 0) - 1;
  proxyCounter.set(epWithPendingListeners, newCount);
  if (newCount === 0) {
    releaseEndpoint(epWithPendingListeners).finally(() => {
      epWithPendingListeners.pendingListeners.clear();
    });
  }
});
function registerProxy(proxy2, epWithPendingListeners) {
  const newCount = (proxyCounter.get(epWithPendingListeners) ?? 0) + 1;
  proxyCounter.set(epWithPendingListeners, newCount);
  if (proxyFinalizers) {
    proxyFinalizers.register(proxy2, epWithPendingListeners, proxy2);
  }
}
function unregisterProxy(proxy2) {
  if (proxyFinalizers) {
    proxyFinalizers.unregister(proxy2);
  }
}
function createProxy(epWithPendingListeners, path = []) {
  let isProxyReleased = false;
  const propProxyCache = /* @__PURE__ */ new Map();
  const proxy2 = new Proxy(function() {
  }, {
    get(_target, prop) {
      throwIfProxyReleased(isProxyReleased);
      if (prop === releaseProxy) {
        return async () => {
          for (const subProxy of propProxyCache.values()) {
            subProxy[releaseProxy]();
          }
          propProxyCache.clear();
          unregisterProxy(proxy2);
          releaseEndpoint(epWithPendingListeners).finally(() => {
            epWithPendingListeners.pendingListeners.clear();
          });
          isProxyReleased = true;
        };
      }
      if (prop === "then") {
        if (path.length === 0) {
          return { then: () => proxy2 };
        }
        const r = requestResponseMessage(epWithPendingListeners, {
          type: MessageType.GET,
          path: path.map((p) => p.toString())
        }).then((v) => fromWireValue(v, epWithPendingListeners.endpoint));
        return r.then.bind(r);
      }
      const cachedProxy = propProxyCache.get(prop);
      if (cachedProxy) {
        return cachedProxy;
      }
      const propProxy = createProxy(epWithPendingListeners, [...path, prop]);
      propProxyCache.set(prop, propProxy);
      return propProxy;
    },
    set(_target, prop, rawValue) {
      throwIfProxyReleased(isProxyReleased);
      const [value, transfer2] = toWireValue(rawValue, epWithPendingListeners.endpoint);
      return requestResponseMessage(epWithPendingListeners, {
        type: MessageType.SET,
        path: [...path, prop].map((p) => p.toString()),
        value
      }, transfer2).then((v) => fromWireValue(v, epWithPendingListeners.endpoint));
    },
    apply(_target, _thisArg, rawArgumentList) {
      throwIfProxyReleased(isProxyReleased);
      const last = path[path.length - 1];
      if (last === "bind") {
        return createProxy(epWithPendingListeners, path.slice(0, -1));
      }
      const [argumentList, transfer2] = processArguments(rawArgumentList, epWithPendingListeners);
      return requestResponseMessage(epWithPendingListeners, {
        type: MessageType.APPLY,
        path: path.map((p) => p.toString()),
        argumentList
      }, transfer2).then((v) => fromWireValue(v, epWithPendingListeners.endpoint));
    },
    construct(_target, rawArgumentList) {
      throwIfProxyReleased(isProxyReleased);
      const [argumentList, transfer2] = processArguments(rawArgumentList, epWithPendingListeners);
      return requestResponseMessage(epWithPendingListeners, {
        type: MessageType.CONSTRUCT,
        path: path.map((p) => p.toString()),
        argumentList
      }, transfer2).then((v) => fromWireValue(v, epWithPendingListeners.endpoint));
    }
  });
  registerProxy(proxy2, epWithPendingListeners);
  return proxy2;
}
var transferCache = /* @__PURE__ */ new WeakMap();
function transfer(obj, transfers) {
  transferCache.set(obj, transfers);
  return obj;
}
function processArguments(argumentList, epWithPendingListeners) {
  const wireValues = [];
  const transferables = [];
  for (const argument of argumentList) {
    const [wireValue, transfer2] = toWireValue(argument, epWithPendingListeners.endpoint);
    wireValues.push(wireValue);
    transferables.push(...transfer2);
  }
  return [wireValues, transferables];
}
function proxy(obj) {
  return Object.assign(obj, { [proxyMarker]: randomId() });
}
function toWireValue(value, ep) {
  for (const [name, handler] of transferHandlers) {
    if (handler.canHandle(value)) {
      const [serializedValue, transfer2] = handler.serialize(value, ep);
      return [{
        type: WireValueType.HANDLER,
        name,
        value: serializedValue
      }, transfer2];
    }
  }
  return [{
    type: WireValueType.RAW,
    value
  }, transferCache.get(value) ?? []];
}
function fromWireValue(value, ep) {
  switch (value.type) {
    case WireValueType.HANDLER:
      return transferHandlers.get(value.name).deserialize(value.value, ep);
    case WireValueType.RAW:
      return value.value;
  }
}
function requestResponseMessage(ep, msg, transfer2 = []) {
  return new Promise((resolve) => {
    const id = randomId();
    ep.pendingListeners.set(id, resolve);
    ep.endpoint.postMessage({ id, ...msg, markerID: ep.rootMarkerID }, transfer2);
  });
}
var hex = [];
var alphabet = "0123456789abcdef";
for (let i = 0; i < 256; i++) {
  hex[i] = alphabet[i >> 4 & 15] + alphabet[i & 15];
}
var step = 0;
var buffer = "";
function randomId() {
  let i = 0;
  if (!buffer || step + 16 > 256 * 2) {
    for (buffer = "", step = 0; i < 256; ++i) {
      buffer += hex[Math.random() * 256 | 0];
    }
  }
  return buffer.substring(step, ++step + 16);
}

// node_modules/abslink/adapters/w3c.js
function createWrapper(channel, messageable) {
  const listeners = /* @__PURE__ */ new WeakMap();
  channel.start?.();
  messageable.start?.();
  return {
    on(event, listener) {
      const unwrapped = (event2) => listener(event2.data);
      if ("addEventListener" in channel) {
        channel.addEventListener(event, unwrapped);
      } else if ("addListener" in channel) {
        channel.addListener(event, unwrapped);
      } else {
        channel.on(event, unwrapped);
      }
      listeners.set(listener, unwrapped);
    },
    off(event, listener) {
      const unwrapped = listeners.get(listener);
      if ("removeEventListener" in channel) {
        channel.removeEventListener(event, unwrapped);
      } else if ("removeListener" in channel) {
        channel.removeListener(event, unwrapped);
      } else {
        channel.off(event, unwrapped);
      }
      listeners.delete(listener);
    },
    postMessage(message, transfer2 = []) {
      messageable.postMessage(message, transfer2);
    }
  };
}
function wrap2(channel, messageable = channel) {
  return wrap(createWrapper(channel, messageable));
}

// node_modules/jassub/dist/debug.js
var import_throughput = __toESM(require_throughput(), 1);
var Debug = class {
  // 5 second average
  fps = (0, import_throughput.default)(5);
  processingDuration = (0, import_throughput.default)(5);
  droppedFrames = 0;
  presentedFrames = 0;
  mistimedFrames = 0;
  _drop() {
    ++this.droppedFrames;
  }
  _startTime = 0;
  _startFrame() {
    this._startTime = performance.now();
  }
  onsubtitleFrameCallback = (_, { fps, processingDuration, droppedFrames }) => console.info("%cFPS: %c%f %c| Frame Time: %c%d ms %c| Dropped Frames: %c%d %c| 5s Avg", "color: #888", "color: #0f0; font-weight: bold", fps.toFixed(1), "color: #888", "color: #0ff; font-weight: bold", processingDuration, "color: #888", "color: #f00; font-weight: bold", droppedFrames, "color: #888");
  _endFrame(meta) {
    ++this.presentedFrames;
    const fps = this.fps(1);
    const now = performance.now();
    const processingDuration = this.processingDuration((now - this._startTime) / fps);
    const frameDelay = Math.max(0, now - meta.expectedDisplayTime);
    if (frameDelay)
      ++this.mistimedFrames;
    this.onsubtitleFrameCallback?.(now, {
      fps,
      processingDuration,
      droppedFrames: this.droppedFrames,
      presentedFrames: this.presentedFrames,
      mistimedFrames: this.mistimedFrames,
      presentationTime: now,
      expectedDisplayTime: meta.expectedDisplayTime + (frameDelay > 0 ? fps / 1e3 : 0),
      frameDelay,
      width: meta.width,
      height: meta.height,
      mediaTime: meta.mediaTime
    });
  }
};

// node_modules/jassub/dist/jassub.js
var webYCbCrMap = {
  rgb: "RGB",
  bt709: "BT709",
  // these might not be exactly correct? oops?
  bt470bg: "BT601",
  // alias BT.601 PAL... whats the difference?
  smpte170m: "BT601"
  // alias BT.601 NTSC... whats the difference?
};
var JASSUB = class _JASSUB {
  timeOffset;
  prescaleFactor;
  prescaleHeightLimit;
  maxRenderHeight;
  debug;
  renderer;
  ready;
  busy = false;
  _video;
  _videoWidth = 0;
  _videoHeight = 0;
  _videoColorSpace = null;
  _canvas;
  _ro = new ResizeObserver(async () => {
    await this.ready;
    this.resize();
  });
  _destroyed = false;
  _lastDemandTime;
  _skipped = false;
  _worker;
  constructor(opts) {
    if (!globalThis.Worker)
      throw new Error("Worker not supported");
    if (!opts)
      throw new Error("No options provided");
    if (!opts.video && !opts.canvas)
      throw new Error("You should give video or canvas in options.");
    _JASSUB._test();
    this.timeOffset = opts.timeOffset ?? 0;
    this._video = opts.video;
    this._canvas = opts.canvas ?? document.createElement("canvas");
    if (this._video && !opts.canvas) {
      this._canvas.className = "JASSUB";
      this._canvas.style.position = "absolute";
      this._canvas.style.pointerEvents = "none";
      this._video.insertAdjacentElement("afterend", this._canvas);
    }
    const ctrl = this._canvas.transferControlToOffscreen();
    this.debug = opts.debug ? new Debug() : null;
    this.prescaleFactor = opts.prescaleFactor ?? 1;
    this.prescaleHeightLimit = opts.prescaleHeightLimit ?? 1080;
    this.maxRenderHeight = opts.maxRenderHeight ?? 0;
    this._worker = opts.workerUrl ? new Worker(opts.workerUrl, { name: "jassub-worker", type: "module" }) : new Worker(new URL("./worker/worker.js", import.meta.url), { name: "jassub-worker", type: "module" });
    const Renderer = wrap2(this._worker);
    const modern = opts.modernWasmUrl ?? new URL("./wasm/jassub-worker-modern.wasm", import.meta.url).href;
    const normal = opts.wasmUrl ?? new URL("./wasm/jassub-worker.wasm", import.meta.url).href;
    const availableFonts = opts.availableFonts ?? {};
    if (!availableFonts["liberation sans"] && !opts.defaultFont) {
      availableFonts["liberation sans"] = new URL("./default.woff2", import.meta.url).href;
    }
    this.ready = new Renderer({
      wasmUrl: _JASSUB._supportsSIMD ? modern : normal,
      width: ctrl.width,
      height: ctrl.height,
      subUrl: opts.subUrl,
      subContent: opts.subContent ?? null,
      fonts: opts.fonts ?? [],
      availableFonts,
      defaultFont: opts.defaultFont ?? "liberation sans",
      debug: !!opts.debug,
      libassMemoryLimit: opts.libassMemoryLimit ?? 0,
      libassGlyphLimit: opts.libassGlyphLimit ?? 0,
      queryFonts: opts.queryFonts ?? "local"
    }, proxy((font) => this._getLocalFont(font)), transfer(ctrl, [ctrl])).then((renderer) => {
      this.renderer = renderer;
    });
    if (this._video) {
      this.setVideo(this._video);
    } else {
      this._ro.observe(this._canvas);
    }
  }
  static _supportsSIMD;
  static _test() {
    if (_JASSUB._supportsSIMD != null)
      return;
    try {
      _JASSUB._supportsSIMD = WebAssembly.validate(Uint8Array.of(
        // WASM magic number + version 1
        0,
        97,
        115,
        109,
        1,
        0,
        0,
        0,
        // Type section: 1 type, func () -> v128
        1,
        5,
        1,
        96,
        0,
        1,
        123,
        // function section 1 function, type index 0
        3,
        2,
        1,
        0,
        // code section 1 body, 0 locals
        10,
        43,
        1,
        41,
        0,
        // v128.const i32x4 0 0 0 0
        253,
        12,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        // v128.const i32x4 0 0 0 0
        253,
        12,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        // i8x16.relaxed_swizzle relaxed SIMD
        253,
        128,
        2,
        // end
        11
      ));
    } catch (e) {
      _JASSUB._supportsSIMD = false;
    }
    const module = new WebAssembly.Module(Uint8Array.of(0, 97, 115, 109, 1, 0, 0, 0));
    if (!(module instanceof WebAssembly.Module) || !(new WebAssembly.Instance(module) instanceof WebAssembly.Instance))
      throw new Error("WASM not supported");
  }
  async resize(forceRepaint = !!this._video?.paused, renderWidth = 0, renderHeight = 0) {
    const videoWidth = this._video?.videoWidth ?? this._videoWidth;
    const videoHeight = this._video?.videoHeight ?? this._videoHeight;
    const videoSize = this._getElementBoundingBox(this._video ?? this._canvas, videoWidth, videoHeight);
    if (!renderWidth || !renderHeight) {
      const widthScale = this._videoWidth / videoWidth || 1;
      const heightScale = this._videoHeight / videoHeight || 1;
      const { width, height } = this._computeRenderSize(videoSize.width * widthScale, videoSize.height * heightScale);
      renderWidth = Math.round(width);
      renderHeight = Math.round(height);
    }
    if (this._video) {
      this._canvas.style.width = Math.round(videoSize.width) + "px";
      this._canvas.style.height = Math.round(videoSize.height) + "px";
      this._canvas.style.top = videoSize.y + "px";
      this._canvas.style.left = videoSize.x + "px";
    }
    await this.renderer._resizeCanvas(renderWidth, renderHeight, this._video ? this._videoWidth || renderWidth : 0, this._video ? this._videoHeight || renderHeight : 0);
    if (this._lastDemandTime)
      await this._demandRender(forceRepaint);
  }
  _getElementBoundingBox(el, videoWidth, videoHeight) {
    const { clientWidth, clientHeight, offsetLeft, offsetTop } = el;
    const videoRatio = videoWidth / videoHeight;
    const elementRatio = clientWidth / clientHeight;
    if (elementRatio > videoRatio) {
      videoHeight = clientHeight;
      videoWidth = clientHeight * videoRatio;
    } else {
      videoHeight = clientWidth / videoRatio;
      videoWidth = clientWidth;
    }
    return { x: offsetLeft + (clientWidth - videoWidth) / 2, y: offsetTop + (clientHeight - videoHeight) / 2, width: videoWidth, height: videoHeight };
  }
  _computeRenderSize(width = 0, height = 0) {
    if (height <= 0 || width <= 0)
      return { width: 0, height: 0 };
    const scalefactor = this.prescaleFactor <= 0 ? 1 : this.prescaleFactor;
    const ratio = self.devicePixelRatio || 1;
    const sgn = scalefactor < 1 ? -1 : 1;
    let newH = height * ratio;
    if (sgn * newH * scalefactor <= sgn * this.prescaleHeightLimit) {
      newH *= scalefactor;
    } else if (sgn * newH < sgn * this.prescaleHeightLimit) {
      newH = this.prescaleHeightLimit;
    }
    if (this.maxRenderHeight > 0 && newH > this.maxRenderHeight)
      newH = this.maxRenderHeight;
    width *= newH / height;
    height = newH;
    return { width, height };
  }
  async setVideo(target) {
    this._removeListeners();
    this._video = target;
    this._ro.observe(target);
    if (typeof VideoFrame !== "undefined") {
      target.addEventListener("loadedmetadata", this._boundUpdateColorSpace);
      this._updateColorSpace({ target });
    }
    await this.ready;
    this._video.requestVideoFrameCallback((now, data) => this._handleRVFC(data));
  }
  async _getLocalFont(font, weight = "regular") {
    if (navigator.permissions?.query) {
      const { state } = await navigator.permissions.query({ name: "local-fonts" });
      if (state !== "granted")
        return;
    }
    for (const data of await self.queryLocalFonts()) {
      const family = data.family.toLowerCase();
      const style = data.style.toLowerCase();
      if (family === font && style === weight) {
        const blob = await data.blob();
        return new Uint8Array(await blob.arrayBuffer());
      }
    }
  }
  _handleRVFC(data) {
    if (this._destroyed)
      return;
    this.manualRender(data);
    this._video.requestVideoFrameCallback((now, data2) => this._handleRVFC(data2));
  }
  manualRender(data, repaint = false) {
    this._lastDemandTime = data;
    return this._demandRender(repaint);
  }
  async _demandRender(repaint = false) {
    const { mediaTime, width, height } = this._lastDemandTime;
    if (width !== this._videoWidth || height !== this._videoHeight) {
      this._videoWidth = width;
      this._videoHeight = height;
      return await this.resize(repaint);
    }
    if (this.busy) {
      this._skipped = true;
      this.debug?._drop();
      return;
    }
    this.busy = true;
    this._skipped = false;
    this.debug?._startFrame();
    await this.renderer._draw(mediaTime + this.timeOffset, repaint);
    this.debug?._endFrame(this._lastDemandTime);
    this.busy = false;
    if (this._skipped)
      await this._demandRender();
  }
  _boundUpdateColorSpace = this._updateColorSpace.bind(this);
  _updateColorSpace({ target }) {
    this._video.requestVideoFrameCallback(async () => {
      if (this._destroyed || this._video !== target)
        return;
      try {
        const frame = new VideoFrame(this._video);
        const colorSpace = webYCbCrMap[frame.colorSpace.matrix];
        frame.close();
        await this.ready;
        await this.renderer._setColorSpace(colorSpace);
      } catch (e) {
        console.warn(e);
      }
    });
  }
  _removeListeners() {
    this._ro.disconnect();
    this._video?.removeEventListener("loadedmetadata", this._boundUpdateColorSpace);
  }
  async destroy() {
    if (this._destroyed)
      return;
    this._destroyed = true;
    this._canvas.remove();
    this._removeListeners();
    try {
      await this.ready;
      await this.renderer?.[releaseProxy]();
    } finally {
      this._worker.terminate();
    }
  }
};
export {
  JASSUB as default,
  webYCbCrMap
};
