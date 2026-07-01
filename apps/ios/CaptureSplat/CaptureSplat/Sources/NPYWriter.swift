import Foundation

enum NPYWriter {
    static func writeFloat32(_ values: [Float], shape: [Int], to url: URL) throws {
        try write(values.withUnsafeBytes { Data($0) }, descr: "<f4", shape: shape, to: url)
    }

    static func writeUInt8(_ values: [UInt8], shape: [Int], to url: URL) throws {
        try write(Data(values), descr: "|u1", shape: shape, to: url)
    }

    private static func write(_ payload: Data, descr: String, shape: [Int], to url: URL) throws {
        let shapeText = "(" + shape.map(String.init).joined(separator: ", ") + (shape.count == 1 ? "," : "") + ")"
        var header = "{'descr': '\(descr)', 'fortran_order': False, 'shape': \(shapeText), }"
        let prefixLength = 10
        let padding = 16 - ((prefixLength + header.utf8.count + 1) % 16)
        header += String(repeating: " ", count: padding) + "\n"

        var data = Data([0x93])
        data.append("NUMPY".data(using: .ascii)!)
        data.append(contentsOf: [0x01, 0x00])
        var headerLength = UInt16(header.utf8.count).littleEndian
        data.append(Data(bytes: &headerLength, count: MemoryLayout<UInt16>.size))
        data.append(header.data(using: .ascii)!)
        data.append(payload)
        try data.write(to: url, options: .atomic)
    }
}
