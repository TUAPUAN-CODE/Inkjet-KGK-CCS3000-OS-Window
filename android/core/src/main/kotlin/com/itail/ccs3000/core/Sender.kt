package com.itail.ccs3000.core

class FieldResult(val module: Int, val ok: Boolean, val errorCode: String?)

object Sender {
    /**
     * Opens ONE connection and sends one STM command per chunk: chunks[i] -> moduleNumbers[i] (a port of send_fields()).
     * A problem while sending a field is recorded for that module and stops the loop (the rest are not sent);
     * a problem while OPENING the connection is thrown (java.io.IOException).
     */
    fun sendFields(
        ip: String,
        port: Int,
        useChecksum: Boolean,
        moduleNumbers: List<Int>,
        chunks: List<String>,
        codeSystem: String,
        timeoutMs: Int = 5000,
        factory: TransportFactory = SocketTransportFactory,
    ): List<FieldResult> {
        require(moduleNumbers.size == chunks.size) {
            "จำนวน module (${moduleNumbers.size}) ไม่ตรงกับจำนวน field (${chunks.size})"
        }

        val results = ArrayList<FieldResult>()
        factory.open(ip, port, timeoutMs).use { conn ->
            for ((moduleNo, chunk) in moduleNumbers.zip(chunks)) {
                try {
                    val wireText = if (codeSystem.uppercase() == "UNICODE") Protocol.textToUnicodeHex(chunk) else chunk
                    val params = Protocol.buildStmContentOnly(moduleNo, wireText, codeSystem)
                    val r = Protocol.sendCommand(conn, "STM", params, useChecksum)
                    results.add(FieldResult(moduleNo, r.ok, r.errorCode))
                } catch (e: Exception) {
                    // catch-all on purpose (like the Python version): an escaping exception would leave the UI stuck on "sending"
                    results.add(FieldResult(moduleNo, false, "${e.javaClass.simpleName}: ${e.message}"))
                    break
                }
            }
        }
        return results
    }

    /** the text sent to every module by "ทดสอบการเชื่อมต่อ": "TEST M<n>", padded / cut to the width of a real field */
    fun testTexts(moduleNumbers: List<Int>, digitsPerField: Int): List<String> =
        moduleNumbers.map { "TEST M$it".padEnd(digitsPerField).take(digitsPerField) }
}
