import AppKit

let output = URL(fileURLWithPath: CommandLine.arguments[1])
let size = 1024
let canvas = NSBitmapImageRep(
    bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size, bitsPerSample: 8,
    samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
    bytesPerRow: 0, bitsPerPixel: 0
)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: canvas)
let tile = NSBezierPath(roundedRect: NSRect(x: 95, y: 95, width: 834, height: 834), xRadius: 195, yRadius: 195)
let gradient = NSGradient(starting: NSColor(srgbRed: 0.08, green: 0.14, blue: 0.23, alpha: 1),
                          ending: NSColor(srgbRed: 0.16, green: 0.28, blue: 0.40, alpha: 1))!
gradient.draw(in: tile, angle: 60)

let colors: [NSColor] = [
    NSColor(srgbRed: 0.47, green: 0.87, blue: 0.81, alpha: 1),
    NSColor(srgbRed: 0.74, green: 0.66, blue: 0.99, alpha: 1),
    NSColor(srgbRed: 0.51, green: 0.75, blue: 0.99, alpha: 1),
]
for (index, color) in colors.enumerated() {
    let x = CGFloat(218 + index * 204)
    color.withAlphaComponent(0.18).setFill()
    NSBezierPath(roundedRect: NSRect(x: x, y: 401, width: 180, height: 285), xRadius: 34, yRadius: 34).fill()
    color.setFill()
    NSBezierPath(roundedRect: NSRect(x: x + 24, y: 617, width: 65, height: 17), xRadius: 8.5, yRadius: 8.5).fill()
    color.withAlphaComponent(0.50).setFill()
    NSBezierPath(roundedRect: NSRect(x: x + 24, y: 559, width: 132, height: 13), xRadius: 6.5, yRadius: 6.5).fill()
    NSBezierPath(roundedRect: NSRect(x: x + 24, y: 530, width: 101, height: 13), xRadius: 6.5, yRadius: 6.5).fill()
}

NSColor.white.withAlphaComponent(0.85).setFill()
NSBezierPath(roundedRect: NSRect(x: 218, y: 310, width: 588, height: 58), xRadius: 23, yRadius: 23).fill()
NSGraphicsContext.restoreGraphicsState()
try canvas.representation(using: .png, properties: [:])!.write(to: output)
