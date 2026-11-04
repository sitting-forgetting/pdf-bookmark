// visionocr — 用 macOS 内置 Vision 框架做中文 OCR，输出 JSON 行框。
// 编译：swiftc -O -o bin/visionocr tools/visionocr.swift -framework Vision -framework AppKit
// 用法：visionocr <image.png> [--level accurate|fast] [--lang zh-Hans,zh-Hant,en-US] [--minconf 0.0]
import Foundation
import Vision
import CoreGraphics
import ImageIO

struct TextLine: Codable {
    var text: String
    var x: Double      // 归一化，左上原点
    var y: Double
    var w: Double
    var h: Double
    var conf: Double
}

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    exit(2)
}

var path: String? = nil
var langs = ["zh-Hans", "zh-Hant", "en-US"]
var level = "accurate"
var minconf = 0.0

var it = CommandLine.arguments.dropFirst().makeIterator()
while let a = it.next() {
    switch a {
    case "--lang":
        if let v = it.next() { langs = v.split(separator: ",").map(String.init) }
    case "--level":
        if let v = it.next() { level = v }
    case "--minconf":
        if let v = it.next(), let d = Double(v) { minconf = d }
    default:
        path = a
    }
}

guard let p = path else { fail("usage: visionocr <image> [--level accurate|fast] [--lang ...] [--minconf 0.0]") }
guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: p) as CFURL, nil),
      let cg = CGImageSourceCreateImageAtIndex(src, 0, nil) else {
    fail("cannot load image: \(p)")
}

let req = VNRecognizeTextRequest()
req.recognitionLevel = (level == "fast") ? .fast : .accurate
req.usesLanguageCorrection = true
req.recognitionLanguages = langs
req.minimumTextHeight = 0.0

let handler = VNImageRequestHandler(cgImage: cg, options: [:])
do {
    try handler.perform([req])
} catch {
    fail("ocr failed: \(error)")
}

var out: [TextLine] = []
for o in (req.results ?? []) {
    guard let c = o.topCandidates(1).first else { continue }
    if Double(c.confidence) < minconf { continue }
    let bb = o.boundingBox
    out.append(TextLine(
        text: c.string,
        x: Double(bb.origin.x),
        y: Double(1.0 - bb.origin.y - bb.size.height),   // Vision 原点在左下，转成左上
        w: Double(bb.size.width),
        h: Double(bb.size.height),
        conf: Double(c.confidence)
    ))
}

out.sort { a, b in (abs(a.y - b.y) > 0.008) ? (a.y < b.y) : (a.x < b.x) }

let enc = JSONEncoder()
enc.outputFormatting = [.withoutEscapingSlashes]
if let data = try? enc.encode(out), let s = String(data: data, encoding: .utf8) {
    print(s)
} else {
    print("[]")
}
