package com.itail.ccs3000

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import com.itail.ccs3000.core.SocketTransportFactory
import com.itail.ccs3000.core.TransportFactory
import java.io.IOException
import java.net.Socket

/**
 * Makes the connection to the printer go through the chosen network. Without this Android sends it through its default network
 * (usually Wi-Fi / mobile data), so a printer reached by an Ethernet cable could not be found while Wi-Fi is on.
 */
object NetworkPicker {
    fun factory(context: Context, choice: String): TransportFactory {
        val cm = context.applicationContext.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        val transport = when (choice) {
            "ethernet" -> NetworkCapabilities.TRANSPORT_ETHERNET
            "wifi" -> NetworkCapabilities.TRANSPORT_WIFI
            else -> null
        }
        if (transport != null) {
            return SocketTransportFactory { socketOn(cm, transport, choice) }
        }
        // auto: prefer a connected Ethernet cable, otherwise the system default
        val eth = findNetwork(cm, NetworkCapabilities.TRANSPORT_ETHERNET)
        return if (eth != null) SocketTransportFactory { eth.socketFactory.createSocket() } else SocketTransportFactory()
    }

    private fun findNetwork(cm: ConnectivityManager, transport: Int) =
        cm.allNetworks.firstOrNull { n -> cm.getNetworkCapabilities(n)?.hasTransport(transport) == true }

    private fun socketOn(cm: ConnectivityManager, transport: Int, name: String): Socket {
        val net = findNetwork(cm, transport)
            ?: throw IOException(
                if (name == "ethernet") "ไม่พบเครือข่าย Ethernet — ตรวจสายแลน/ตัวแปลง USB-LAN และ IP ของแท็บเล็ต (ต้องอยู่วงเดียวกับเครื่องพิมพ์)"
                else "ไม่พบเครือข่าย Wi-Fi — เชื่อมต่อ Wi-Fi ก่อน",
            )
        return net.socketFactory.createSocket()
    }
}
