package com.itail.ccs3000.core

/** a scanned value that cannot be sent (nothing is sent to the printer); the message is the Thai text shown to the user */
class PayloadException(message: String) : Exception(message)

class SplitResult(
    val chunks: List<String>,
    val actualDataLength: Int,
    val trailingSpaceCount: Int,
    val addedSpaceCount: Int,
)

/** Port of the payload handling of ccs3000_app.py: clean, pad with trailing spaces, split into fields. */
object Payload {
    /** Removes only the scanner line-ending characters (CR / LF). Spaces are NOT stripped: trailing spaces are part of the payload. */
    fun cleanScannerInput(payload: String?): String = (payload ?: "").replace("\r", "").replace("\n", "")

    /** adds trailing spaces until the payload has [expectedLen] characters; returns the padded text and how many spaces were added */
    fun padPayloadToLength(payload: String, expectedLen: Int): Pair<String, Int> {
        val clean = cleanScannerInput(payload)
        val current = clean.length
        if (current == expectedLen) return clean to 0
        if (current > expectedLen) {
            throw PayloadException("ข้อมูลยาวเกินกำหนด: ได้ $current ตัว แต่ต้องการไม่เกิน $expectedLen ตัว")
        }
        val add = expectedLen - current
        return (clean + " ".repeat(add)) to add
    }

    /** Validates and splits the payload into [fieldCount] fields of [digitsPerField] characters (pads with trailing spaces first). */
    fun splitPayload(payload: String, fieldCount: Int, digitsPerField: Int): SplitResult {
        val expected = fieldCount * digitsPerField
        val (padded, added) = padPayloadToLength(cleanScannerInput(payload), expected)

        if (padded.length != expected) {
            throw PayloadException("ความยาวข้อมูลไม่ถูกต้อง: ได้ ${padded.length} ตัว แต่ต้องการ $expected ตัว ($fieldCount field × $digitsPerField ตัว)")
        }

        val trailing = padded.length - padded.trimEnd(' ').length
        val actual = padded.length - trailing
        if (padded.substring(0, actual).isEmpty()) throw PayloadException("ไม่พบข้อมูลจริงใน QR Code")

        val chunks = (0 until expected step digitsPerField).map { padded.substring(it, it + digitsPerField) }
        return SplitResult(chunks, actual, trailing, added)
    }

    /** how the text read so far will be split (nothing is sent); null when it is empty or already too long */
    fun previewChunks(value: String, fieldCount: Int, digitsPerField: Int): List<String>? {
        val expected = fieldCount * digitsPerField
        if (value.isEmpty() || value.length > expected) return null
        val padded = value + " ".repeat(expected - value.length)
        return (0 until expected step digitsPerField).map { padded.substring(it, it + digitsPerField) }
    }

    /** "497,498, 499" -> [497, 498, 499]; throws PayloadException with the Thai message */
    fun parseModuleNumbers(text: String?): List<Int> {
        val parts = (text ?: "").split(",").map { it.trim() }.filter { it.isNotEmpty() }
        if (parts.isEmpty()) throw PayloadException("กรุณาระบุหมายเลข module อย่างน้อย 1 ตัว")
        return parts.map { p ->
            if (!p.all { it.isDigit() }) throw PayloadException("หมายเลข module ต้องเป็นตัวเลขเท่านั้น (พบ: '$p')")
            val n = p.toIntOrNull() ?: throw PayloadException("หมายเลข module ต้องอยู่ระหว่าง 1-500 (พบ: $p)")
            if (n !in 1..500) throw PayloadException("หมายเลข module ต้องอยู่ระหว่าง 1-500 (พบ: $n)")
            n
        }
    }
}
