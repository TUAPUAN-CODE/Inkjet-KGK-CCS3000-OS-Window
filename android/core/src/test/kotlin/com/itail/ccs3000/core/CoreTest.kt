package com.itail.ccs3000.core

import java.net.ServerSocket
import kotlin.concurrent.thread
import kotlin.test.Test
import kotlin.test.assertContentEquals
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

private fun ByteArray.hex() = joinToString("") { "%02X".format(it) }

class ProtocolTest {
    // vectors produced by ccs3000_comm.py (the Windows version)
    @Test fun frameWithChecksum() {
        val p = "0:497::4" + Protocol.textToUnicodeHex("A1 ") + ":"
        assertEquals("0253544D3A303A3439373A3A343030343130303331303032303A0369", Protocol.buildFrame("STM", p, true).hex())
    }

    @Test fun frameWithoutChecksum() {
        val p = "0:497::4" + Protocol.textToUnicodeHex("A1 ") + ":"
        assertEquals("0253544D3A303A3439373A3A343030343130303331303032303A03", Protocol.buildFrame("STM", p, false).hex())
    }

    @Test fun unicodeHex() {
        assertEquals("004100310E01", Protocol.textToUnicodeHex("A1ก"))
    }

    @Test fun stmContentOnly() {
        assertEquals("0:497::4ABC:", Protocol.buildStmContentOnly(497, "ABC"))
        assertEquals("0:1::3ABC:", Protocol.buildStmContentOnly(1, "ABC", "ASCII+SHIFT_JIS"))
    }

    @Test fun nonAsciiParametersRejected() {
        assertFailsWith<IllegalArgumentException> { Protocol.buildFrame("STM", "0:1::4ก:") }
        assertFailsWith<IllegalArgumentException> { Protocol.buildFrame("ST", "x") }
    }
}

class PayloadTest {
    @Test fun cleanKeepsSpaces() = assertEquals("AB  ", Payload.cleanScannerInput("AB  \r\n"))

    @Test fun padAndSplit() {
        val r = Payload.splitPayload("ABC", 4, 5)
        assertEquals(listOf("ABC  ", "     ", "     ", "     "), r.chunks)
        assertEquals(3, r.actualDataLength)
        assertEquals(17, r.addedSpaceCount)
    }

    @Test fun tooLong() {
        assertFailsWith<PayloadException> { Payload.splitPayload("A".repeat(21), 4, 5) }
    }

    @Test fun emptyRejected() {
        assertFailsWith<PayloadException> { Payload.splitPayload("", 4, 5) }
        assertFailsWith<PayloadException> { Payload.splitPayload("   ", 4, 5) }
    }

    @Test fun preview() {
        assertNull(Payload.previewChunks("", 2, 3))
        assertNull(Payload.previewChunks("ABCDEFG", 2, 3))
        assertEquals(listOf("ABC", "D  "), Payload.previewChunks("ABCD", 2, 3))
    }

    @Test fun modules() {
        assertEquals(listOf(497, 498, 499), Payload.parseModuleNumbers("497, 498,499,"))
        assertFailsWith<PayloadException> { Payload.parseModuleNumbers("") }
        assertFailsWith<PayloadException> { Payload.parseModuleNumbers("1,x") }
        assertFailsWith<PayloadException> { Payload.parseModuleNumbers("0") }
        assertFailsWith<PayloadException> { Payload.parseModuleNumbers("501") }
    }
}

class SettingsTest {
    private fun input(port: String = "1024", digits: String = "40", msg: String = "999", modules: String = "497,498") =
        SettingsInput("10.0.0.5", port, digits, msg, "UNICODE", modules, true, true)

    @Test fun valid() {
        val (cfg, err) = Settings.validate(input(), AppConfig())
        assertNull(err)
        assertEquals(listOf(497, 498), cfg!!.moduleNumbers)
        assertEquals(80, cfg.expectedLength)
    }

    @Test fun invalid() {
        assertTrue(Settings.validate(input(port = "x"), AppConfig()).second != null)
        assertTrue(Settings.validate(input(port = "70000"), AppConfig()).second != null)
        assertTrue(Settings.validate(input(digits = "0"), AppConfig()).second != null)
        assertTrue(Settings.validate(input(msg = "1000"), AppConfig()).second != null)
        assertTrue(Settings.validate(input(modules = "a"), AppConfig()).second != null)
    }
}

/** a fake printer on a real local socket: replies ACK (or NAK + error code) to every frame */
class SenderTest {
    private fun fakePrinter(nakModule: Int? = null, received: MutableList<ByteArray>): Pair<ServerSocket, Thread> {
        val server = ServerSocket(0)
        val t = thread(isDaemon = true) {
            server.accept().use { s ->
                val input = s.getInputStream()
                val out = s.getOutputStream()
                while (true) {
                    val buf = ArrayList<Byte>()
                    var b = input.read()
                    if (b < 0) break
                    buf.add(b.toByte())
                    // read up to ETX then the checksum byte
                    while (b != Protocol.ETX) { b = input.read(); if (b < 0) return@use; buf.add(b.toByte()) }
                    buf.add(input.read().toByte())
                    val frame = buf.toByteArray()
                    received.add(frame)
                    val text = String(frame, 1, frame.size - 3, Charsets.US_ASCII)
                    val module = text.split(":")[2].toInt()
                    if (module == nakModule) {
                        out.write(byteArrayOf(Protocol.NAK.toByte(), Protocol.STX.toByte()) + "1570H".toByteArray() + byteArrayOf(Protocol.ETX.toByte()))
                    } else out.write(Protocol.ACK)
                    out.flush()
                }
            }
        }
        return server to t
    }

    @Test fun sendsAllFields() {
        val rx = java.util.Collections.synchronizedList(ArrayList<ByteArray>())
        val (server, _) = fakePrinter(null, rx)
        server.use {
            val res = Sender.sendFields("127.0.0.1", server.localPort, true, listOf(497, 498), listOf("A", "B"), "UNICODE", 2000)
            assertTrue(res.all { it.ok })
            assertEquals(2, res.size)
        }
        assertEquals(2, rx.size)
        assertContentEquals(Protocol.buildFrame("STM", "0:497::40041:"), rx[0])
    }

    @Test fun nakStopsNothingButIsRecorded() {
        val rx = java.util.Collections.synchronizedList(ArrayList<ByteArray>())
        val (server, _) = fakePrinter(498, rx)
        server.use {
            val res = Sender.sendFields("127.0.0.1", server.localPort, true, listOf(497, 498, 499), listOf("A", "B", "C"), "UNICODE", 2000)
            assertTrue(res[0].ok)
            assertFalse(res[1].ok)
            assertEquals("1570H", res[1].errorCode)
            assertTrue(res[2].ok)
        }
    }

    @Test fun connectFailureThrows() {
        val port = ServerSocket(0).use { it.localPort } // closed again -> nothing listens
        assertFailsWith<java.io.IOException> { Sender.sendFields("127.0.0.1", port, true, listOf(1), listOf("A"), "UNICODE", 500) }
    }

    @Test fun testTexts() {
        assertEquals(listOf("TEST M497 ", "TEST M1   "), Sender.testTexts(listOf(497, 1), 10))
        assertEquals(listOf("TEST"), Sender.testTexts(listOf(497), 4))
    }
}
