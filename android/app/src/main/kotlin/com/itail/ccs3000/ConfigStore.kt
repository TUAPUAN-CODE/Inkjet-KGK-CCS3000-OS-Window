package com.itail.ccs3000

import android.content.Context
import com.itail.ccs3000.core.AppConfig

/** keeps the settings in SharedPreferences (the Windows version keeps them in config.json) */
class ConfigStore(context: Context) {
    private val prefs = context.applicationContext.getSharedPreferences("ccs3000", Context.MODE_PRIVATE)

    fun load(): AppConfig {
        val d = AppConfig()
        val modules = prefs.getString("modules", null)
            ?.split(",")?.mapNotNull { it.trim().toIntOrNull() }?.takeIf { it.isNotEmpty() }
            ?: d.moduleNumbers
        return AppConfig(
            printerIp = prefs.getString("ip", d.printerIp) ?: d.printerIp,
            printerPort = prefs.getInt("port", d.printerPort),
            useChecksum = prefs.getBoolean("checksum", d.useChecksum),
            moduleNumbers = modules,
            messageNo = prefs.getInt("messageNo", d.messageNo),
            digitsPerField = prefs.getInt("digits", d.digitsPerField),
            codeSystem = prefs.getString("codeSystem", d.codeSystem) ?: d.codeSystem,
            cameraFacing = prefs.getString("camera", d.cameraFacing) ?: d.cameraFacing,
            hideSoftKeyboard = prefs.getBoolean("hideKb", d.hideSoftKeyboard),
            network = prefs.getString("network", d.network) ?: d.network,
        )
    }

    fun save(c: AppConfig) {
        prefs.edit()
            .putString("ip", c.printerIp).putInt("port", c.printerPort).putBoolean("checksum", c.useChecksum)
            .putString("modules", c.moduleNumbers.joinToString(",")).putInt("messageNo", c.messageNo)
            .putInt("digits", c.digitsPerField).putString("codeSystem", c.codeSystem)
            .putString("network", c.network).putString("camera", c.cameraFacing).putBoolean("hideKb", c.hideSoftKeyboard)
            .apply()
    }
}
