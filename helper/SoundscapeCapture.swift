// Soundscape capture helper.
//
// Records what the Mac is playing (system audio, via a Core Audio process tap; macOS 14.2+)
// or what an input device hears (microphone, audio interface, loopback device) into a mono
// 16-bit WAV file, and streams JSON-line events (status, level meter) on stdout.
// Audio only: no screen recording, no video.
//
//   soundscape-capture record --source output|input [--device NAME|UID] --out FILE.wav
//                             [--seconds N] [--watch-stdin] [--level-interval 0.1]
//   soundscape-capture devices
//   soundscape-capture permission [--request system_audio|microphone|all]
//   soundscape-capture version

import AVFoundation
import AudioToolbox
import CoreAudio
import Darwin
import Foundation

let helperVersion = "0.1.0"

// MARK: - Events (JSON lines on stdout)

let outLock = NSLock()

func emit(_ event: String, _ fields: [String: Any] = [:]) {
    var obj = fields
    obj["event"] = event
    guard JSONSerialization.isValidJSONObject(obj),
          let data = try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])
    else { return }
    outLock.lock()
    defer { outLock.unlock() }
    var line = data
    line.append(0x0A)
    line.withUnsafeBytes { raw in
        var off = 0
        while off < raw.count {
            let n = Darwin.write(1, raw.baseAddress!.advanced(by: off), raw.count - off)
            if n <= 0 { break }
            off += n
        }
    }
}

func die(_ message: String, code: String = "error", exitCode: Int32 = 1, _ fields: [String: Any] = [:]) -> Never {
    var f = fields
    f["message"] = message
    f["code"] = code
    emit("error", f)
    exit(exitCode)
}

func db(_ v: Double) -> Double {
    guard v > 1e-6 else { return -120 }
    return max(-120, (20 * log10(v) * 10).rounded() / 10)
}

// MARK: - Responsibility disclaim
//
// macOS attributes privacy permissions (microphone, system audio) to the "responsible"
// process, which for a child of Claude/Terminal is that parent app. We re-spawn ourselves
// disclaiming responsibility so the permission belongs to Soundscape Capture.app itself and
// works the same no matter which app launched the MCP server.

typealias DisclaimFn = @convention(c) (UnsafeMutablePointer<posix_spawnattr_t?>, Int32) -> Int32

func respawnDisclaimedIfNeeded() {
    let env = ProcessInfo.processInfo.environment
    if env["SOUNDSCAPE_DISCLAIMED"] != nil || env["SOUNDSCAPE_NO_DISCLAIM"] != nil { return }
    guard let sym = dlsym(UnsafeMutableRawPointer(bitPattern: -2), "responsibility_spawnattrs_setdisclaim"),
          let exe = Bundle.main.executablePath
    else { return }
    let disclaim = unsafeBitCast(sym, to: DisclaimFn.self)

    var attr: posix_spawnattr_t? = nil
    posix_spawnattr_init(&attr)
    defer { posix_spawnattr_destroy(&attr) }
    guard disclaim(&attr, 1) == 0 else { return }

    var childEnv = env
    childEnv["SOUNDSCAPE_DISCLAIMED"] = "1"
    let args = [exe] + CommandLine.arguments.dropFirst()
    var argv: [UnsafeMutablePointer<CChar>?] = args.map { strdup($0) } + [nil]
    var envp: [UnsafeMutablePointer<CChar>?] = childEnv.map { strdup("\($0.key)=\($0.value)") } + [nil]
    defer {
        argv.forEach { free($0) }
        envp.forEach { free($0) }
    }

    var pid: pid_t = 0
    guard posix_spawn(&pid, exe, nil, &attr, &argv, &envp) == 0 else { return }

    var sources: [DispatchSourceSignal] = []
    for sig in [SIGINT, SIGTERM, SIGHUP] {
        signal(sig, SIG_IGN)
        let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
        src.setEventHandler { kill(pid, sig) }
        src.resume()
        sources.append(src)
    }
    DispatchQueue.global().async {
        var status: Int32 = 0
        while waitpid(pid, &status, 0) < 0 && errno == EINTR {}
        let termSig = status & 0x7f
        exit(termSig == 0 ? (status >> 8) & 0xff : 128 + termSig)
    }
    withExtendedLifetime(sources) { dispatchMain() }
}

// MARK: - Permissions

enum TCC {
    typealias PreflightFn = @convention(c) (CFString, CFDictionary?) -> Int
    typealias RequestFn = @convention(c) (CFString, CFDictionary?, @escaping @convention(block) (Bool) -> Void) -> Void

    static let handle = dlopen("/System/Library/PrivateFrameworks/TCC.framework/Versions/A/TCC", RTLD_NOW)
    static let service = "kTCCServiceAudioCapture" as CFString

    static func systemAudioStatus() -> String {
        guard let h = handle, let sym = dlsym(h, "TCCAccessPreflight") else { return "unknown" }
        switch unsafeBitCast(sym, to: PreflightFn.self)(service, nil) {
        case 0: return "granted"
        case 1: return "denied"
        default: return "undetermined"
        }
    }

    static func requestSystemAudio(timeout: Double = 120) -> String {
        guard let h = handle, let sym = dlsym(h, "TCCAccessRequest") else { return systemAudioStatus() }
        let sem = DispatchSemaphore(value: 0)
        var granted = false
        unsafeBitCast(sym, to: RequestFn.self)(service, nil) { ok in
            granted = ok
            sem.signal()
        }
        if sem.wait(timeout: .now() + timeout) == .timedOut { return "undetermined" }
        return granted ? "granted" : "denied"
    }
}

func microphoneStatus() -> String {
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized: return "granted"
    case .denied: return "denied"
    case .restricted: return "restricted"
    case .notDetermined: return "undetermined"
    @unknown default: return "unknown"
    }
}

func requestMicrophone(timeout: Double = 120) -> String {
    if microphoneStatus() != "undetermined" { return microphoneStatus() }
    let sem = DispatchSemaphore(value: 0)
    AVCaptureDevice.requestAccess(for: .audio) { _ in sem.signal() }
    _ = sem.wait(timeout: .now() + timeout)
    return microphoneStatus()
}

// MARK: - Core Audio property helpers

func address(_ sel: AudioObjectPropertySelector, _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal)
    -> AudioObjectPropertyAddress
{
    AudioObjectPropertyAddress(mSelector: sel, mScope: scope, mElement: kAudioObjectPropertyElementMain)
}

func getValue<T>(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector, _ initial: T,
                 scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> T?
{
    var addr = address(sel, scope)
    var size = UInt32(MemoryLayout<T>.size)
    var value = initial
    let status = withUnsafeMutableBytes(of: &value) { AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, $0.baseAddress!) }
    return status == noErr ? value : nil
}

func getString(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector) -> String? {
    var addr = address(sel)
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    var value: Unmanaged<CFString>? = nil
    guard AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, &value) == noErr, let v = value else { return nil }
    return v.takeRetainedValue() as String
}

func allDevices() -> [AudioObjectID] {
    var addr = address(kAudioHardwarePropertyDevices)
    var size: UInt32 = 0
    let system = AudioObjectID(kAudioObjectSystemObject)
    guard AudioObjectGetPropertyDataSize(system, &addr, 0, nil, &size) == noErr else { return [] }
    var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    guard AudioObjectGetPropertyData(system, &addr, 0, nil, &size, &ids) == noErr else { return [] }
    return ids
}

func channelCount(_ dev: AudioObjectID, scope: AudioObjectPropertyScope) -> Int {
    var addr = address(kAudioDevicePropertyStreamConfiguration, scope)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(dev, &addr, 0, nil, &size) == noErr, size > 0 else { return 0 }
    let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: MemoryLayout<AudioBufferList>.alignment)
    defer { raw.deallocate() }
    guard AudioObjectGetPropertyData(dev, &addr, 0, nil, &size, raw) == noErr else { return 0 }
    let abl = UnsafeMutableAudioBufferListPointer(raw.assumingMemoryBound(to: AudioBufferList.self))
    return abl.reduce(0) { $0 + Int($1.mNumberChannels) }
}

func defaultDevice(_ sel: AudioObjectPropertySelector) -> AudioObjectID? {
    let id = getValue(AudioObjectID(kAudioObjectSystemObject), sel, AudioObjectID(kAudioObjectUnknown))
    return (id == nil || id == AudioObjectID(kAudioObjectUnknown)) ? nil : id
}

func nominalRate(_ dev: AudioObjectID) -> Double {
    getValue(dev, kAudioDevicePropertyNominalSampleRate, Float64(0)) ?? 0
}

func transportName(_ dev: AudioObjectID) -> String {
    let t = getValue(dev, kAudioDevicePropertyTransportType, UInt32(0)) ?? 0
    switch t {
    case kAudioDeviceTransportTypeBuiltIn: return "built-in"
    case kAudioDeviceTransportTypeUSB: return "usb"
    case kAudioDeviceTransportTypeBluetooth, kAudioDeviceTransportTypeBluetoothLE: return "bluetooth"
    case kAudioDeviceTransportTypeVirtual: return "virtual"
    case kAudioDeviceTransportTypeAggregate: return "aggregate"
    case kAudioDeviceTransportTypeHDMI, kAudioDeviceTransportTypeDisplayPort: return "display"
    case kAudioDeviceTransportTypeAirPlay: return "airplay"
    case kAudioDeviceTransportTypeThunderbolt: return "thunderbolt"
    default: return "other"
    }
}

func deviceInfo(_ dev: AudioObjectID) -> [String: Any] {
    [
        "id": Int(dev),
        "name": getString(dev, kAudioObjectPropertyName) ?? "?",
        "uid": getString(dev, kAudioDevicePropertyDeviceUID) ?? "",
        "input_channels": channelCount(dev, scope: kAudioObjectPropertyScopeInput),
        "output_channels": channelCount(dev, scope: kAudioObjectPropertyScopeOutput),
        "sample_rate": nominalRate(dev),
        "transport": transportName(dev),
    ]
}

func findInputDevice(_ query: String?) -> AudioObjectID? {
    guard let q = query, !q.isEmpty, q.lowercased() != "default" else {
        return defaultDevice(kAudioHardwarePropertyDefaultInputDevice)
    }
    let inputs = allDevices().filter { channelCount($0, scope: kAudioObjectPropertyScopeInput) > 0 }
    if let exact = inputs.first(where: { getString($0, kAudioDevicePropertyDeviceUID) == q }) { return exact }
    let lq = q.lowercased()
    if let exact = inputs.first(where: { getString($0, kAudioObjectPropertyName)?.lowercased() == lq }) { return exact }
    return inputs.first(where: { (getString($0, kAudioObjectPropertyName) ?? "").lowercased().contains(lq) })
}

// MARK: - WAV sink: downmix to mono, write 16-bit PCM, meter levels

final class WavSink {
    let file: UnsafeMutablePointer<FILE>
    let sampleRate: Double
    let levelFrames: Int
    private(set) var frames: Int64 = 0
    private var framesAtHeader: Int64 = 0
    private var sumSq = 0.0
    private var peak: Float = 0
    private var meterCount = 0
    private var everNonZero = false
    private var scratch = [Int16]()
    private var mono = [Float]()

    init(path: String, sampleRate: Double, levelInterval: Double) {
        guard let f = fopen(path, "wb") else { die("cannot open \(path) for writing", code: "io") }
        file = f
        self.sampleRate = sampleRate
        levelFrames = max(64, Int(sampleRate * levelInterval))
        writeHeader()
    }

    private func writeHeader() {
        let dataBytes = UInt32(clamping: frames * 2)
        var h = Data()
        func u32(_ v: UInt32) { withUnsafeBytes(of: v.littleEndian) { h.append(contentsOf: $0) } }
        func u16(_ v: UInt16) { withUnsafeBytes(of: v.littleEndian) { h.append(contentsOf: $0) } }
        h.append(contentsOf: Array("RIFF".utf8)); u32(36 &+ dataBytes)
        h.append(contentsOf: Array("WAVE".utf8))
        h.append(contentsOf: Array("fmt ".utf8)); u32(16); u16(1); u16(1)
        u32(UInt32(sampleRate)); u32(UInt32(sampleRate) * 2); u16(2); u16(16)
        h.append(contentsOf: Array("data".utf8)); u32(dataBytes)
        fseek(file, 0, SEEK_SET)
        h.withUnsafeBytes { _ = fwrite($0.baseAddress, 1, $0.count, file) }
        fseek(file, 0, SEEK_END)
        fflush(file)
        framesAtHeader = frames
    }

    /// Consume Float32 buffers (interleaved or not, any channel count) and append mono PCM.
    func consume(_ ablPtr: UnsafePointer<AudioBufferList>) {
        let abl = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: ablPtr))
        var n = Int.max
        var totalChannels = 0
        for buf in abl where buf.mData != nil && buf.mNumberChannels > 0 {
            n = min(n, Int(buf.mDataByteSize) / (4 * Int(buf.mNumberChannels)))
            totalChannels += Int(buf.mNumberChannels)
        }
        guard totalChannels > 0, n > 0, n != Int.max else { return }
        if mono.count < n { mono = [Float](repeating: 0, count: n); scratch = [Int16](repeating: 0, count: n) }
        for i in 0..<n { mono[i] = 0 }
        for buf in abl where buf.mData != nil && buf.mNumberChannels > 0 {
            let ch = Int(buf.mNumberChannels)
            let p = buf.mData!.assumingMemoryBound(to: Float.self)
            for i in 0..<n {
                var s: Float = 0
                for c in 0..<ch { s += p[i * ch + c] }
                mono[i] += s
            }
        }
        let scale = 1 / Float(totalChannels)
        for i in 0..<n {
            let v = mono[i] * scale
            let a = abs(v)
            if a > peak { peak = a }
            if a > 0 { everNonZero = true }
            sumSq += Double(v * v)
            meterCount += 1
            scratch[i] = Int16(max(-1, min(1, v)) * 32767)
            if meterCount >= levelFrames {
                emit("level", [
                    "t": (Double(frames + Int64(i + 1)) / sampleRate * 100).rounded() / 100,
                    "rms_db": db((sumSq / Double(meterCount)).squareRoot()),
                    "peak_db": db(Double(peak)),
                ])
                sumSq = 0; peak = 0; meterCount = 0
            }
        }
        scratch.withUnsafeBytes { _ = fwrite($0.baseAddress, 2, n, file) }
        frames += Int64(n)
        if frames - framesAtHeader >= Int64(sampleRate / 4) { writeHeader() }
    }

    var hadSignal: Bool { everNonZero }

    func finish() {
        writeHeader()
        fclose(file)
    }
}

// MARK: - Capture sources

protocol CaptureSource {
    var sampleRate: Double { get }
    var description: [String: Any] { get }
    func start(queue: DispatchQueue, sink: WavSink) throws
    func stop()
}

struct CaptureError: Error { let message: String; let code: String }

func check(_ status: OSStatus, _ what: String, code: String = "coreaudio") throws {
    if status != noErr { throw CaptureError(message: "\(what) failed (OSStatus \(status))", code: code) }
}

@available(macOS 14.2, *)
final class SystemOutputTap: CaptureSource {
    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggregateID = AudioObjectID(kAudioObjectUnknown)
    private var procID: AudioDeviceIOProcID?
    private(set) var sampleRate: Double = 48000
    private var outputName = "?"
    private var channels = 2

    var description: [String: Any] {
        ["source": "output", "device": "System audio (all apps) via \(outputName)", "channels": channels]
    }

    func prepare() throws {
        let desc = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
        desc.uuid = UUID()
        desc.name = "Soundscape"
        desc.isPrivate = true
        desc.muteBehavior = .unmuted
        try check(AudioHardwareCreateProcessTap(desc, &tapID), "AudioHardwareCreateProcessTap", code: "tap")

        var fmt = AudioStreamBasicDescription()
        var addr = address(kAudioTapPropertyFormat)
        var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        try check(AudioObjectGetPropertyData(tapID, &addr, 0, nil, &size, &fmt), "read tap format", code: "tap")
        if fmt.mFormatFlags & kAudioFormatFlagIsFloat == 0 || fmt.mBitsPerChannel != 32 {
            throw CaptureError(message: "unexpected tap format (flags \(fmt.mFormatFlags), bits \(fmt.mBitsPerChannel))", code: "format")
        }
        channels = Int(fmt.mChannelsPerFrame)

        guard let outDev = defaultDevice(kAudioHardwarePropertyDefaultSystemOutputDevice)
            ?? defaultDevice(kAudioHardwarePropertyDefaultOutputDevice),
            let outUID = getString(outDev, kAudioDevicePropertyDeviceUID)
        else { throw CaptureError(message: "no default output device", code: "no_device") }
        outputName = getString(outDev, kAudioObjectPropertyName) ?? "output"

        let aggregate: [String: Any] = [
            kAudioAggregateDeviceNameKey: "Soundscape Tap",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            kAudioAggregateDeviceMainSubDeviceKey: outUID,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outUID]],
            kAudioAggregateDeviceTapListKey: [[
                kAudioSubTapDriftCompensationKey: true,
                kAudioSubTapUIDKey: desc.uuid.uuidString,
            ]],
        ]
        try check(AudioHardwareCreateAggregateDevice(aggregate as CFDictionary, &aggregateID),
                  "AudioHardwareCreateAggregateDevice", code: "tap")
        let aggRate = nominalRate(aggregateID)
        sampleRate = aggRate > 0 ? aggRate : fmt.mSampleRate
    }

    func start(queue: DispatchQueue, sink: WavSink) throws {
        try check(AudioDeviceCreateIOProcIDWithBlock(&procID, aggregateID, queue) { _, input, _, _, _ in
            sink.consume(input)
        }, "AudioDeviceCreateIOProcIDWithBlock", code: "tap")
        try check(AudioDeviceStart(aggregateID, procID), "AudioDeviceStart", code: "tap")
    }

    func stop() {
        if let p = procID {
            AudioDeviceStop(aggregateID, p)
            AudioDeviceDestroyIOProcID(aggregateID, p)
            procID = nil
        }
        if aggregateID != kAudioObjectUnknown { AudioHardwareDestroyAggregateDevice(aggregateID) }
        if tapID != kAudioObjectUnknown { AudioHardwareDestroyProcessTap(tapID) }
        aggregateID = AudioObjectID(kAudioObjectUnknown)
        tapID = AudioObjectID(kAudioObjectUnknown)
    }
}

final class InputDevice: CaptureSource {
    private let deviceID: AudioObjectID
    private var procID: AudioDeviceIOProcID?
    let sampleRate: Double

    init(_ id: AudioObjectID) {
        deviceID = id
        sampleRate = nominalRate(id)
    }

    var description: [String: Any] {
        var d = deviceInfo(deviceID)
        d["source"] = "input"
        d["device"] = d["name"]
        return d
    }

    func start(queue: DispatchQueue, sink: WavSink) throws {
        var addr = address(kAudioDevicePropertyStreamFormat, kAudioObjectPropertyScopeInput)
        var fmt = AudioStreamBasicDescription()
        var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        if AudioObjectGetPropertyData(deviceID, &addr, 0, nil, &size, &fmt) == noErr,
           fmt.mFormatFlags & kAudioFormatFlagIsFloat == 0 || fmt.mBitsPerChannel != 32
        {
            throw CaptureError(message: "unsupported input format (flags \(fmt.mFormatFlags), bits \(fmt.mBitsPerChannel))", code: "format")
        }
        try check(AudioDeviceCreateIOProcIDWithBlock(&procID, deviceID, queue) { _, input, _, _, _ in
            sink.consume(input)
        }, "AudioDeviceCreateIOProcIDWithBlock")
        try check(AudioDeviceStart(deviceID, procID), "AudioDeviceStart")
    }

    func stop() {
        if let p = procID {
            AudioDeviceStop(deviceID, p)
            AudioDeviceDestroyIOProcID(deviceID, p)
            procID = nil
        }
    }
}

// MARK: - Commands

func option(_ name: String, in args: [String]) -> String? {
    guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
    return args[i + 1]
}

func cmdDevices() {
    let devices = allDevices().map(deviceInfo)
    let uid = { (sel: AudioObjectPropertySelector) -> String in
        defaultDevice(sel).flatMap { getString($0, kAudioDevicePropertyDeviceUID) } ?? ""
    }
    emit("devices", [
        "devices": devices,
        "default_input": uid(kAudioHardwarePropertyDefaultInputDevice),
        "default_output": uid(kAudioHardwarePropertyDefaultOutputDevice),
    ])
}

func cmdPermission(_ args: [String]) {
    let req = option("--request", in: args) ?? ""
    var system = TCC.systemAudioStatus()
    var mic = microphoneStatus()
    if (req == "system_audio" || req == "all") && system != "granted" { system = TCC.requestSystemAudio() }
    if (req == "microphone" || req == "all") && mic == "undetermined" { mic = requestMicrophone() }
    emit("permission", ["system_audio": system, "microphone": mic])
}

var activeSource: CaptureSource?
var activeSink: WavSink?
let ioQueue = DispatchQueue(label: "soundscape.capture.io", qos: .userInteractive)
var stopping = false
let stopLock = NSLock()

func stopAndExit(_ reason: String) -> Never {
    stopLock.lock()
    if stopping { stopLock.unlock(); while true { sleep(1) } }
    stopping = true
    stopLock.unlock()
    activeSource?.stop()
    var fields: [String: Any] = ["reason": reason]
    ioQueue.sync {
        if let sink = activeSink {
            sink.finish()
            fields["frames"] = Int(sink.frames)
            fields["seconds"] = (Double(sink.frames) / sink.sampleRate * 100).rounded() / 100
            fields["had_signal"] = sink.hadSignal
        }
    }
    emit("stopped", fields)
    exit(0)
}

func cmdRecord(_ args: [String]) -> Never {
    let sourceName = option("--source", in: args) ?? "output"
    guard let out = option("--out", in: args) else { die("--out FILE.wav is required", code: "usage") }
    let seconds = Double(option("--seconds", in: args) ?? "") ?? 0
    let levelInterval = Double(option("--level-interval", in: args) ?? "") ?? 0.1

    let source: CaptureSource
    switch sourceName {
    case "output":
        guard #available(macOS 14.2, *) else {
            die("System audio capture needs macOS 14.2+. Install a loopback device (e.g. BlackHole) and record it with --source input --device BlackHole.",
                code: "unsupported_os")
        }
        var perm = TCC.systemAudioStatus()
        if perm == "undetermined" || perm == "unknown" {
            emit("permission_prompt", ["kind": "system_audio"])
            perm = TCC.requestSystemAudio()
        }
        if perm == "denied" {
            die("System audio recording permission is denied for Soundscape Capture. Enable it in System Settings > Privacy & Security > Screen & System Audio Recording (System Audio Recording Only).",
                code: "permission_denied", exitCode: 3, ["kind": "system_audio"])
        }
        let tap = SystemOutputTap()
        do { try tap.prepare() } catch let e as CaptureError {
            die(e.message, code: e.code)
        } catch { die("\(error)") }
        source = tap
    case "input":
        var perm = microphoneStatus()
        if perm == "undetermined" {
            emit("permission_prompt", ["kind": "microphone"])
            perm = requestMicrophone()
        }
        if perm == "denied" || perm == "restricted" {
            die("Microphone permission is denied for Soundscape Capture. Enable it in System Settings > Privacy & Security > Microphone.",
                code: "permission_denied", exitCode: 3, ["kind": "microphone"])
        }
        guard let dev = findInputDevice(option("--device", in: args)) else {
            die("No matching input device. Run `soundscape-capture devices` to list them.", code: "no_device")
        }
        source = InputDevice(dev)
    default:
        die("--source must be 'output' or 'input'", code: "usage")
    }

    let sink = WavSink(path: out, sampleRate: source.sampleRate, levelInterval: levelInterval)
    activeSource = source
    activeSink = sink

    for sig in [SIGINT, SIGTERM, SIGHUP] {
        signal(sig, SIG_IGN)
        let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
        src.setEventHandler { stopAndExit("signal") }
        src.resume()
        signalSources.append(src)
    }
    if args.contains("--watch-stdin") {
        Thread.detachNewThread {
            var buf = [UInt8](repeating: 0, count: 256)
            while read(0, &buf, buf.count) > 0 {}
            DispatchQueue.main.async { stopAndExit("stdin_closed") }
        }
    }
    if seconds > 0 {
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { stopAndExit("duration") }
    }

    do { try source.start(queue: ioQueue, sink: sink) } catch let e as CaptureError {
        source.stop()
        die(e.message, code: e.code)
    } catch { die("\(error)") }

    var started = source.description
    started["sample_rate"] = source.sampleRate
    started["path"] = out
    started["format"] = "wav_s16_mono"
    started["pid"] = Int(getpid())
    emit("started", started)
    dispatchMain()
}

var signalSources: [DispatchSourceSignal] = []

// MARK: - Main

signal(SIGPIPE, SIG_IGN)
let args = Array(CommandLine.arguments.dropFirst())
let command = args.first ?? "help"

switch command {
case "version":
    emit("version", ["version": helperVersion, "macos": ProcessInfo.processInfo.operatingSystemVersionString])
    exit(0)
case "devices":
    cmdDevices()
    exit(0)
case "permission":
    respawnDisclaimedIfNeeded()
    cmdPermission(args)
    exit(0)
case "record":
    respawnDisclaimedIfNeeded()
    cmdRecord(args)
default:
    FileHandle.standardError.write("""
    soundscape-capture \(helperVersion)
      record --source output|input [--device NAME|UID] --out FILE.wav [--seconds N] [--watch-stdin]
      devices
      permission [--request system_audio|microphone|all]
      version

    """.data(using: .utf8)!)
    exit(command == "help" ? 0 : 2)
}
