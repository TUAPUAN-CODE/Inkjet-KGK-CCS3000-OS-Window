package com.itail.ccs3000.core

/** the settings of the app (same keys and defaults as DEFAULT_CONFIG of the Windows version, plus the two tablet options) */
data class AppConfig(
    val printerIp: String = "192.168.1.110",
    val printerPort: Int = 1024,
    val useChecksum: Boolean = true,
    val moduleNumbers: List<Int> = listOf(497, 498, 499, 500),
    val messageNo: Int = 999,
    val digitsPerField: Int = 40,
    val codeSystem: String = "UNICODE",
    /** "back" | "front" — the camera used to scan */
    val cameraFacing: String = "back",
    /** hide the on-screen keyboard when the scan box is focused (a USB / Bluetooth scanner types by itself) */
    val hideSoftKeyboard: Boolean = true,
    /** "auto" | "ethernet" | "wifi" — which network the connection to the printer must use */
    val network: String = "auto",
) {
    val fieldCount: Int get() = moduleNumbers.size
    val expectedLength: Int get() = moduleNumbers.size * digitsPerField
}

/** an error shown to the user: [title] is the dialog / banner title, [message] the detail (Thai) */
class SettingsError(val title: String, val message: String)

/** what the settings form returns before it is validated */
class SettingsInput(
    val printerIp: String,
    val printerPort: String,
    val digitsPerField: String,
    val messageNo: String,
    val codeSystem: String,
    val moduleNumbers: String,
    val useChecksum: Boolean,
    val hideSoftKeyboard: Boolean,
    val network: String = "auto",
)

object Settings {
    val NETWORKS = listOf("auto", "ethernet", "wifi")
    val CODE_SYSTEMS = listOf("UNICODE", "ASCII", "SHIFT_JIS")

    /** Validates the settings form (unsaved values included). Returns the new config, or the error to show. */
    fun validate(input: SettingsInput, base: AppConfig): Pair<AppConfig?, SettingsError?> {
        val port = input.printerPort.trim().toIntOrNull()
        val digits = input.digitsPerField.trim().toIntOrNull()
        val messageNo = input.messageNo.trim().toIntOrNull()
        if (port == null || digits == null || messageNo == null) {
            return null to SettingsError("ข้อมูลไม่ถูกต้อง", "Port / ตัวอักษรต่อ field / Message no. ต้องเป็นตัวเลข")
        }
        val ip = input.printerIp.trim()
        if (ip.isEmpty()) return null to SettingsError("ข้อมูลไม่ถูกต้อง", "กรุณาระบุ Printer IP")
        if (port !in 1..65535) return null to SettingsError("ข้อมูลไม่ถูกต้อง", "Port ต้องอยู่ระหว่าง 1-65535")
        if (digits < 1) return null to SettingsError("ข้อมูลไม่ถูกต้อง", "ตัวอักษร / field ต้องมากกว่า 0")
        if (messageNo !in 0..999) {
            return null to SettingsError(
                "หมายเลข message ไม่ถูกต้อง",
                "Message no. ต้องอยู่ระหว่าง 0-999 (คือเลขที่มุมซ้ายบนของหน้าจอเครื่องตอนสร้าง module เช่น 999)",
            )
        }
        val modules = try {
            Payload.parseModuleNumbers(input.moduleNumbers)
        } catch (e: PayloadException) {
            return null to SettingsError("หมายเลข module ไม่ถูกต้อง", e.message ?: "")
        }
        val cfg = base.copy(
            printerIp = ip, printerPort = port, digitsPerField = digits, messageNo = messageNo,
            codeSystem = if (input.codeSystem in CODE_SYSTEMS) input.codeSystem else base.codeSystem,
            moduleNumbers = modules, useChecksum = input.useChecksum, hideSoftKeyboard = input.hideSoftKeyboard,
            network = if (input.network in NETWORKS) input.network else "auto",
        )
        return cfg to null
    }
}
