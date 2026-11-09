package com.itail.ccs3000

import android.app.Application
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.itail.ccs3000.core.AppConfig
import com.itail.ccs3000.core.FieldResult
import com.itail.ccs3000.core.Payload
import com.itail.ccs3000.core.PayloadException
import com.itail.ccs3000.core.Sender
import com.itail.ccs3000.core.Settings
import com.itail.ccs3000.core.SettingsError
import com.itail.ccs3000.core.SettingsInput
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

enum class StatusKind { READY, BUSY, OK, ERROR }

class Status(val kind: StatusKind, val title: String, val detail: String = "")

/** one row of the result table */
class ResultRow(val module: Int, val text: String, val ok: Boolean?, val note: String)

class MainViewModel(app: Application) : AndroidViewModel(app) {
    private val store = ConfigStore(app)

    var config by mutableStateOf(store.load()); private set
    var scanText by mutableStateOf(""); private set
    var status by mutableStateOf(Status(StatusKind.READY, "พร้อมสแกน", "")); private set
    var sending by mutableStateOf(false); private set
    var testing by mutableStateOf(false); private set
    var rows by mutableStateOf<List<ResultRow>>(emptyList()); private set
    var canResend by mutableStateOf(false); private set
    /** settings form error (title, message) shown as a dialog */
    var dialogError by mutableStateOf<SettingsError?>(null); private set
    val log = mutableStateListOf<String>()

    private var lastChunks: List<String>? = null
    private var autoSendJob: Job? = null
    private var readyResetJob: Job? = null

    init {
        status = readyStatus()
    }

    private fun readyStatus() = Status(
        StatusKind.READY, "พร้อมสแกน",
        "สแกนข้อมูล ${config.expectedLength} ตัว (${config.fieldCount} field × ${config.digitsPerField}) แล้วระบบจะส่งเข้า module ${config.moduleNumbers.joinToString(", ")}",
    )

    private fun now() = SimpleDateFormat("HH:mm:ss", Locale.US).format(Date())

    private fun addLog(line: String) {
        log.add(0, "${now()}  $line")
        while (log.size > 300) log.removeAt(log.size - 1)
    }

    fun dismissError() { dialogError = null }

    // ------------------------------------------------------------------ scan input

    /** called on every change of the scan box (typing, scanner, camera). Enter characters submit at once. */
    fun onScanChanged(raw: String) {
        if (sending) return
        val submit = raw.contains('\n') || raw.contains('\r')
        val value = Payload.cleanScannerInput(raw)
        scanText = value
        autoSendJob?.cancel()
        readyResetJob?.cancel()

        val expected = config.expectedLength
        if (value.isEmpty()) { status = readyStatus(); return }
        if (value.length > expected) {
            status = Status(StatusKind.ERROR, "ข้อมูลยาวเกินกำหนด", "ได้ ${value.length} ตัว แต่ต้องการไม่เกิน $expected ตัว — กดล้างแล้วสแกนใหม่")
            return
        }
        if (submit) { process(value); return }

        // scanners type very fast: a short pause means the scan is finished even without a trailing Enter
        val debounce = if (value.length == expected) 80L else 200L
        status = if (value.length == expected) Status(StatusKind.BUSY, "อ่านข้อมูลครบแล้ว", "กำลังเตรียมส่ง...")
        else Status(StatusKind.BUSY, "กำลังอ่านข้อมูล...", "อ่านแล้ว ${value.length}/$expected ตัว (ที่เหลือจะเติมช่องว่างท้ายให้)")
        autoSendJob = viewModelScope.launch {
            delay(debounce)
            if (scanText == value) process(value)
        }
    }

    /** a keyboard Enter (hardware scanner / IME action) */
    fun submitScan() {
        if (sending) return
        autoSendJob?.cancel()
        val v = scanText
        if (v.isNotEmpty()) process(v)
    }

    /** a value read by the camera is processed right away */
    fun onCameraScanned(text: String) {
        if (sending) return
        autoSendJob?.cancel()
        val v = Payload.cleanScannerInput(text)
        scanText = v
        process(v)
    }

    fun clearScan() {
        autoSendJob?.cancel()
        readyResetJob?.cancel()
        scanText = ""
        rows = emptyList()
        status = readyStatus()
    }

    // ------------------------------------------------------------------ send

    private fun process(value: String) {
        val split = try {
            Payload.splitPayload(value, config.fieldCount, config.digitsPerField)
        } catch (e: PayloadException) {
            status = Status(StatusKind.ERROR, "ข้อมูลไม่ถูกต้อง", e.message ?: "")
            addLog("✖ ${e.message}")
            return
        }
        addLog(
            "สแกน: ${split.actualDataLength} ตัว" +
                (if (split.addedSpaceCount > 0) " (เติมช่องว่างท้าย ${split.addedSpaceCount} ตัว)" else ""),
        )
        send(split.chunks)
    }

    fun resendLast() {
        val chunks = lastChunks ?: return
        if (sending) return
        addLog("ส่งซ้ำข้อมูลล่าสุด")
        send(chunks)
    }

    private fun send(chunks: List<String>) {
        val cfg = config
        lastChunks = chunks
        canResend = true
        sending = true
        rows = cfg.moduleNumbers.zip(chunks).map { (m, c) -> ResultRow(m, c, null, "กำลังส่ง...") }
        status = Status(StatusKind.BUSY, "กำลังส่งไปเครื่องพิมพ์...", "${cfg.printerIp}:${cfg.printerPort}")

        viewModelScope.launch {
            var error: String? = null
            val results: List<FieldResult> = withContext(Dispatchers.IO) {
                try {
                    Sender.sendFields(cfg.printerIp, cfg.printerPort, cfg.useChecksum, cfg.moduleNumbers, chunks, cfg.codeSystem)
                } catch (e: Exception) {
                    error = "เชื่อมต่อเครื่องพิมพ์ไม่ได้ (${cfg.printerIp}:${cfg.printerPort}): ${e.message ?: e.javaClass.simpleName}"
                    emptyList()
                }
            }
            finishSend(cfg, chunks, results, error)
        }
    }

    private fun finishSend(cfg: AppConfig, chunks: List<String>, results: List<FieldResult>, error: String?) {
        sending = false
        if (error != null) {
            rows = cfg.moduleNumbers.zip(chunks).map { (m, c) -> ResultRow(m, c, false, "ไม่ได้ส่ง") }
            status = Status(StatusKind.ERROR, "ส่งไม่สำเร็จ", error)
            addLog("✖ $error")
            return
        }
        val byModule = results.associateBy { it.module }
        rows = cfg.moduleNumbers.zip(chunks).map { (m, c) ->
            val r = byModule[m]
            when {
                r == null -> ResultRow(m, c, false, "ไม่ได้ส่ง")
                r.ok -> ResultRow(m, c, true, "ACK")
                else -> ResultRow(m, c, false, r.errorCode?.let { "NAK $it" } ?: "NAK")
            }
        }
        val okCount = results.count { it.ok }
        if (okCount == chunks.size) {
            status = Status(StatusKind.OK, "ส่งสำเร็จ", "ส่งครบ ${chunks.size} field")
            addLog("✔ ส่งสำเร็จ ${chunks.size} field → module ${cfg.moduleNumbers.joinToString(", ")}")
            scanText = ""
            readyResetJob = viewModelScope.launch {
                delay(5000)
                status = readyStatus()
            }
        } else {
            val bad = rows.filter { it.ok != true }.joinToString(", ") { "M${it.module}: ${it.note}" }
            status = Status(StatusKind.ERROR, "ส่งไม่ครบ ($okCount/${chunks.size})", bad)
            addLog("✖ ส่งไม่ครบ ($okCount/${chunks.size}) $bad")
        }
    }

    // ------------------------------------------------------------------ settings

    /** validates the form and saves; returns true when saved */
    fun saveSettings(input: SettingsInput): Boolean {
        val (cfg, err) = Settings.validate(input, config)
        if (cfg == null) { dialogError = err; return false }
        store.save(cfg)
        config = cfg
        status = readyStatus()
        addLog("บันทึกการตั้งค่า (${cfg.printerIp}:${cfg.printerPort}, module ${cfg.moduleNumbers.joinToString(",")})")
        return true
    }

    fun setCameraFacing(facing: String) {
        val cfg = config.copy(cameraFacing = facing)
        store.save(cfg)
        config = cfg
    }

    /** sends "TEST M<n>" to EVERY module with the (possibly unsaved) form values; the UI confirms first */
    fun testConnection(input: SettingsInput) {
        if (testing || sending) return
        val (cfg, err) = Settings.validate(input, config)
        if (cfg == null) { dialogError = err; return }
        testing = true
        addLog("ทดสอบการเชื่อมต่อ ${cfg.printerIp}:${cfg.printerPort} ...")
        viewModelScope.launch {
            var error: String? = null
            val results = withContext(Dispatchers.IO) {
                try {
                    Sender.sendFields(
                        cfg.printerIp, cfg.printerPort, cfg.useChecksum, cfg.moduleNumbers,
                        Sender.testTexts(cfg.moduleNumbers, cfg.digitsPerField), cfg.codeSystem,
                    )
                } catch (e: Exception) {
                    error = e.message ?: e.javaClass.simpleName
                    emptyList()
                }
            }
            testing = false
            if (error != null) {
                dialogError = SettingsError("เชื่อมต่อไม่ได้", "เชื่อมต่อ ${cfg.printerIp}:${cfg.printerPort} ไม่สำเร็จ\n$error")
                addLog("✖ ทดสอบไม่ผ่าน: $error")
            } else if (results.size == cfg.fieldCount && results.all { it.ok }) {
                dialogError = SettingsError("เชื่อมต่อสำเร็จ", "เครื่องตอบ ACK ครบทุก module (${cfg.moduleNumbers.joinToString(", ")})")
                addLog("✔ ทดสอบผ่าน ทุก module ACK")
            } else {
                val bad = results.filter { !it.ok }.joinToString("\n") { "M${it.module}: ${it.errorCode ?: "NAK"}" }
                dialogError = SettingsError("ทดสอบไม่ครบ", "บาง module ไม่ตอบ ACK\n$bad")
                addLog("✖ ทดสอบไม่ครบ: $bad")
            }
        }
    }
}
