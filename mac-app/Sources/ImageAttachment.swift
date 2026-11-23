import AppKit
import ImageIO
import SwiftUI
import UniformTypeIdentifiers

struct ImageAttachment: Codable, Equatable, Identifiable {
    let id = UUID()
    let name: String
    let data: Data

    enum CodingKeys: String, CodingKey {
        case name, data
    }

    static let maxCount = 4
    static let maxBytes = 5 * 1024 * 1024
    static let maxTotalBytes = 10 * 1024 * 1024

    enum Source {
        case file(URL)
        case clipboard(Data)
    }

    static func load(_ input: Source, language: InterfaceLanguage) throws -> Self {
        let source: CGImageSource?
        let name: String
        let options = [kCGImageSourceShouldCache: false] as CFDictionary
        switch input {
        case .file(let url):
            source = CGImageSourceCreateWithURL(url as CFURL, options)
            name = url.lastPathComponent
        case .clipboard(let data):
            source = CGImageSourceCreateWithData(data as CFData, options)
            name = language.text("粘贴的图片", "Pasted image")
        }
        guard let source, let image = thumbnail(source, maxPixels: 3072) else {
            throw ImportError(language.text("无法读取图片「\(name)」", "Could not read image “\(name)”"))
        }

        if case .file(let url) = input,
           let type = CGImageSourceGetType(source) as String?,
           [UTType.png, .jpeg, .gif, .webP].contains(where: { $0.identifier == type }),
           let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
           let width = properties[kCGImagePropertyPixelWidth] as? Int,
           let height = properties[kCGImagePropertyPixelHeight] as? Int,
           max(width, height) <= 3072,
           let size = try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize, size <= maxBytes,
           let original = try? Data(contentsOf: url), original.count <= maxBytes {
            return Self(name: name, data: original)
        }

        let transparent = [.first, .last, .premultipliedFirst, .premultipliedLast].contains(image.alphaInfo)
        let preferPNG = transparent || CGImageSourceGetType(source) as String? == UTType.png.identifier
        if preferPNG, let data = encode(image, as: .png), data.count <= maxBytes {
            return Self(name: name, data: data)
        }
        if !transparent, let data = encode(image, as: .jpeg), data.count <= maxBytes {
            return Self(name: name, data: data)
        }
        if let smaller = thumbnail(source, maxPixels: 1536),
           let data = encode(smaller, as: transparent ? .png : .jpeg), data.count <= maxBytes {
            return Self(name: name, data: data)
        }
        throw ImportError(language.text("图片「\(name)」超过 5 MiB", "Image “\(name)” exceeds 5 MiB"))
    }

    var thumbnail: CGImage? {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil) else { return nil }
        return Self.thumbnail(source, maxPixels: 200)
    }

    private static func thumbnail(_ source: CGImageSource, maxPixels: Int) -> CGImage? {
        CGImageSourceCreateThumbnailAtIndex(source, 0, [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: maxPixels,
        ] as CFDictionary)
    }

    private static func encode(_ image: CGImage, as type: UTType) -> Data? {
        let data = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(data, type.identifier as CFString, 1, nil) else {
            return nil
        }
        CGImageDestinationAddImage(destination, image, [kCGImageDestinationLossyCompressionQuality: 0.9] as CFDictionary)
        return CGImageDestinationFinalize(destination) ? data as Data : nil
    }

    private struct ImportError: LocalizedError {
        let message: String
        init(_ message: String) { self.message = message }
        var errorDescription: String? { message }
    }
}

struct ImageAttachmentsRow: View {
    @Environment(\.interfaceLanguage) private var language
    let images: [ImageAttachment]
    var remove: ((ImageAttachment.ID) -> Void)?

    var body: some View {
        ScrollView(.horizontal) {
            HStack(spacing: 8) {
                ForEach(images) { image in
                    ZStack(alignment: .topTrailing) {
                        AttachmentThumbnail(image: image)
                            .equatable()
                        if let remove {
                            Button {
                                remove(image.id)
                            } label: {
                                Image(systemName: "xmark.circle.fill")
                                    .font(.system(size: 16))
                                    .symbolRenderingMode(.palette)
                                    .foregroundStyle(.white, .black.opacity(0.75))
                            }
                            .buttonStyle(.plain)
                            .padding(3)
                            .accessibilityLabel(language.text("移除 \(image.name)", "Remove \(image.name)"))
                        }
                    }
                }
            }
        }
        .frame(height: 56)
        .scrollIndicators(.hidden)
    }
}

private struct AttachmentThumbnail: View, Equatable {
    let image: ImageAttachment

    static func == (lhs: Self, rhs: Self) -> Bool { lhs.image.id == rhs.image.id }

    var body: some View {
        Group {
            if let thumbnail = image.thumbnail {
                Image(decorative: thumbnail, scale: 1, orientation: .up)
                    .resizable()
                    .scaledToFit()
            } else {
                Image(systemName: "photo")
                    .foregroundStyle(.secondary)
            }
        }
        .frame(width: 74, height: 56)
        .background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 6))
        .clipShape(RoundedRectangle(cornerRadius: 6))
        .help(image.name)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(image.name)
        .accessibilityAddTraits(.isImage)
    }
}
