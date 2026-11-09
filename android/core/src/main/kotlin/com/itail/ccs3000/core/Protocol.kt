package com.itail.ccs3000.core

import java.io.Closeable
import java.io.IOException
import java.net.InetSocketAddress
import java.net.Socket
import java.net.SocketTimeoutException

/**
 * CCS3000 (KGK JET) LAN communication — a Kotlin port of ccs3000_comm.py (the same protocol, byte for byte).
 *
 * Frame:   STX(02H) + CMD(3 chars) + ":" + Parameters + ETX(03H) [+ Checksum(1 byte)]
 * Checksum: low-order byte of the sum of every byte between STX and ETX (CMD ":" Parameters), STX / ETX not included.
 * Reply:   ACK (06H) ok · NAK (15H) rejected · NAK + STX + ErrorCode + ETX rejected with a code · ACK + STX + Data + ETX for G-commands.
 *
 * Verified against the printer in the Python version (see the comments there): the ":" after the CMD is required, and the first field of
 * STM is the fixed "command version" 0 — NOT a message number.
 */
object Protocol {
    const val STX = 0x02
    const val ETX = 0x03
    const val ACK = 0x06
    const val NAK = 0x15

    /** the code system digit of the STM command */
    val CODE_SYSTEM: Map<String, String> = linkedMapOf(
        "JIS" to "0", "ASCII" to "1", "SHIFT_JIS" to "2", "ASCII+SHIFT_JIS" to "3",
        "UNICODE" to "4", "GB" to "6", "ASCII+GB" to "7", "KS" to "8",
        "ASCII+KS" to "9", "BIG5" to "A", "ASCII+BIG5" to "B",
    )

    fun calcChecksum(payload: ByteArray): Int = payload.sumOf { it.toInt() and 0xFF } and 0xFF

    /** CMD must be exactly 3 ASCII characters and the whole payload ASCII (like Python's .encode("ascii")) */
    fun buildFrame(cmd: String, parameters: String, useChecksum: Boolean = true): ByteArray {
        require(cmd.length == 3) { "CMD must be exactly 3 characters, e.g. 'STM'" }
        val text = "$cmd:$parameters"
        val bad = text.firstOrNull { it.code > 0x7F }
        if (bad != null) {
            throw IllegalArgumentException("'ascii' codec can't encode character '$bad' (U+${bad.code.toString(16).uppercase().padStart(4, '0')})")
        }
        val payload = text.toByteArray(Charsets.US_ASCII)
        val frame = ArrayList<Byte>(payload.size + 3)
        frame.add(STX.toByte())
        payload.forEach { frame.add(it) }
        frame.add(ETX.toByte())
        if (useChecksum) frame.add(calcChecksum(payload).toByte())
        return frame.toByteArray()
    }

    /** the parameters of STM that change ONLY the printed content of a Text module: "0:<module>::<code system digit><text>:" */
    fun buildStmContentOnly(moduleNo: Int, text: String, codeSystem: String = "UNICODE"): String {
        val code = CODE_SYSTEM[codeSystem.uppercase()] ?: throw NoSuchElementException(codeSystem)
        return "0:$moduleNo::$code$text:"
    }

    /** UNICODE modules expect every character as its 4-digit hex code point ("A1" -> "00410031"); plain text would be ACKed but print nothing */
    fun textToUnicodeHex(text: String): String {
        val sb = StringBuilder()
        text.codePoints().forEach { sb.append(it.toString(16).uppercase().padStart(4, '0')) }
        return sb.toString()
    }

    /** the printer's answer to one command */
    class CommandResult(val ok: Boolean, val data: String?, val errorCode: String?, val raw: ByteArray)

    /**
     * Sends one command and waits for the answer. Like the Python version, after the first byte it waits a short time (0.3 s)
     * for the data / error-code part that may follow.
     */
    fun sendCommand(t: PrinterTransport, cmd: String, parameters: String, useChecksum: Boolean = true): CommandResult {
        t.write(buildFrame(cmd, parameters, useChecksum))
        val first = t.readFirstByte()
        if (first < 0) throw IOException("Printer closed the connection")
        return when (first) {
            ACK -> {
                val rest = t.readRest()
                val data = if (rest.isNotEmpty() && (rest[0].toInt() and 0xFF) == STX) extractBetweenStxEtx(rest) else null
                CommandResult(true, data, null, byteArrayOf(first.toByte()) + rest)
            }
            NAK -> {
                val rest = t.readRest()
                val err = if (rest.isNotEmpty() && (rest[0].toInt() and 0xFF) == STX) extractBetweenStxEtx(rest) else null
                CommandResult(false, null, err, byteArrayOf(first.toByte()) + rest)
            }
            else -> throw IllegalStateException("Unexpected first byte from printer: 0x${first.toString(16).uppercase().padStart(2, '0')}")
        }
    }

    private fun extractBetweenStxEtx(rest: ByteArray): String {
        val end = rest.indexOf(ETX.toByte())
        if (end < 0) throw IllegalStateException("ETX not found in the printer's answer")
        return String(rest, 1, end - 1, Charsets.US_ASCII)
    }
}

/** the byte pipe to the printer (a socket, or a fake in the tests) */
interface PrinterTransport {
    fun write(bytes: ByteArray)

    /** the first byte of the answer, waiting the normal timeout (throws on timeout); -1 when the printer closed the connection */
    fun readFirstByte(): Int

    /** whatever else arrives within a short time (0.3 s); empty when nothing came */
    fun readRest(): ByteArray
}

interface PrinterConnection : PrinterTransport, Closeable

fun interface TransportFactory {
    /** opens ONE connection; throws IOException when the printer cannot be reached */
    fun open(ip: String, port: Int, timeoutMs: Int): PrinterConnection
}

class SocketConnection(private val socket: Socket, private val timeoutMs: Int) : PrinterConnection {
    private val input = socket.getInputStream()
    private val output = socket.getOutputStream()

    init { socket.soTimeout = timeoutMs }

    override fun write(bytes: ByteArray) {
        output.write(bytes)
        output.flush()
    }

    override fun readFirstByte(): Int {
        socket.soTimeout = timeoutMs
        return input.read()
    }

    override fun readRest(): ByteArray {
        socket.soTimeout = SHORT_WAIT_MS
        return try {
            val buf = ByteArray(4096)
            val n = input.read(buf)
            if (n <= 0) ByteArray(0) else buf.copyOf(n)
        } catch (_: SocketTimeoutException) {
            ByteArray(0)
        } finally {
            socket.soTimeout = timeoutMs
        }
    }

    override fun close() = socket.close()

    companion object { const val SHORT_WAIT_MS = 300 }
}

/** [newSocket] lets the app hand out sockets bound to a chosen network (Ethernet / Wi-Fi); the default uses the system's default route */
class SocketTransportFactory(private val newSocket: () -> Socket = { Socket() }) : TransportFactory {
    override fun open(ip: String, port: Int, timeoutMs: Int): PrinterConnection {
        val socket = newSocket()
        try {
            socket.tcpNoDelay = true
            socket.connect(InetSocketAddress(ip, port), timeoutMs)
        } catch (e: IOException) {
            runCatching { socket.close() }
            throw e
        }
        return SocketConnection(socket, timeoutMs)
    }
}
