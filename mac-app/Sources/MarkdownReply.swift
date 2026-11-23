import SwiftUI

struct MarkdownReply: View, Equatable {
    let source: String

    private struct Paragraph {
        let intent: PresentationIntent?
        var content: AttributedString

        var table: (id: Int, columns: Int)? {
            for component in intent?.components ?? [] {
                if case .table(let columns) = component.kind { return (component.identity, columns.count) }
            }
            return nil
        }

        var tablePosition: (row: Int, column: Int)? {
            guard let components = intent?.components, components.count >= 2,
                  case .tableCell(let column) = components[0].kind else { return nil }
            switch components[1].kind {
            case .tableHeaderRow: return (0, column)
            case .tableRow(let row): return (row, column)
            default: return nil
            }
        }
    }

    private enum Block {
        case paragraph(Paragraph)
        case table([[AttributedString]])
    }

    var body: some View {
        let blocks = parse()
        VStack(alignment: .leading, spacing: 9) {
            ForEach(blocks.indices, id: \.self) { index in
                switch blocks[index] {
                case .paragraph(let paragraph): styled(paragraph)
                case .table(let rows): table(rows)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .textSelection(.enabled)
    }

    private func parse() -> [Block] {
        guard var formatted = try? AttributedString(markdown: source) else {
            return [.paragraph(Paragraph(intent: nil, content: AttributedString(source)))]
        }
        for run in formatted.runs where run.inlinePresentationIntent?.contains(.code) == true {
            formatted[run.range].font = .system(.body, design: .monospaced)
            formatted[run.range].backgroundColor = .secondary.opacity(0.12)
        }
        var paragraphs: [Paragraph] = []
        for run in formatted.runs {
            let content = AttributedString(formatted[run.range])
            if let last = paragraphs.indices.last, paragraphs[last].intent == run.presentationIntent {
                paragraphs[last].content.append(content)
            } else {
                paragraphs.append(Paragraph(intent: run.presentationIntent, content: content))
            }
        }

        var blocks: [Block] = []
        var index = 0
        while index < paragraphs.count {
            let paragraph = paragraphs[index]
            if let table = paragraph.table {
                let emptyRow = Array(repeating: AttributedString(), count: table.columns)
                var rows: [[AttributedString]] = []
                while index < paragraphs.count, paragraphs[index].table?.id == table.id {
                    let cell = paragraphs[index]
                    if let (row, column) = cell.tablePosition, emptyRow.indices.contains(column) {
                        while rows.count <= row { rows.append(emptyRow) }
                        rows[row][column] = cell.content
                    }
                    index += 1
                }
                blocks.append(.table(rows))
            } else {
                blocks.append(.paragraph(paragraph))
                index += 1
            }
        }
        return blocks
    }

    @ViewBuilder
    private func styled(_ paragraph: Paragraph) -> some View {
        let components = paragraph.intent?.components ?? []
        if case .header(let level) = components.first?.kind {
            Text(paragraph.content).font((level <= 2 ? Font.title3 : .headline).weight(.semibold))
        } else if case .codeBlock(let language) = components.first?.kind {
            VStack(alignment: .leading, spacing: 6) {
                if let language { Text(language).font(.caption).foregroundStyle(.secondary) }
                ScrollView(.horizontal) {
                    let code = String(paragraph.content.characters)
                    Text(verbatim: code.hasSuffix("\n") ? String(code.dropLast()) : code)
                        .font(.system(.body, design: .monospaced))
                        .fixedSize(horizontal: true, vertical: false)
                        .padding(.bottom, 8)
                        .compactEnclosingScrollIndicators()
                }
            }
            .padding([.top, .horizontal], 10)
            .padding(.bottom, 2)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.quaternary.opacity(0.48), in: RoundedRectangle(cornerRadius: 6))
        } else if let item = components.firstIndex(where: {
            if case .listItem = $0.kind { return true }
            return false
        }), case .listItem(let ordinal) = components[item].kind {
            let ordered = components.indices.contains(item + 1) && components[item + 1].kind == .orderedList
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(ordered ? "\(ordinal)." : "•").foregroundStyle(.secondary)
                Text(paragraph.content)
            }
        } else if components.contains(where: { $0.kind == .blockQuote }) {
            Text(paragraph.content)
                .foregroundStyle(.secondary)
                .padding(.leading, 10)
                .overlay(alignment: .leading) { Rectangle().fill(.quaternary).frame(width: 2) }
        } else {
            Text(paragraph.content)
        }
    }

    private func table(_ rows: [[AttributedString]]) -> some View {
        ScrollView(.horizontal) {
            Grid(alignment: .leading, horizontalSpacing: 0, verticalSpacing: 0) {
                ForEach(rows.indices, id: \.self) { row in
                    GridRow {
                        ForEach(rows[row].indices, id: \.self) { column in
                            Text(rows[row][column])
                                .font(row == 0 ? .subheadline.weight(.semibold) : .subheadline)
                                .padding(8)
                                .frame(minWidth: 84, maxWidth: 190, alignment: .leading)
                        }
                    }
                    .background(row.isMultiple(of: 2) ? Color.secondary.opacity(0.09) : .clear)
                }
            }
            .fixedSize(horizontal: true, vertical: false)
            .compactEnclosingScrollIndicators()
        }
    }
}
