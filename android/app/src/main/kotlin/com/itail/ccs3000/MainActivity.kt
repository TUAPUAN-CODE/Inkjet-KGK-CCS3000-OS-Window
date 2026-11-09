package com.itail.ccs3000

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Tab
import androidx.compose.material3.TabRow
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.input.key.KeyEventType
import androidx.compose.ui.input.key.key
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.input.key.type
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.itail.ccs3000.core.AppConfig
import com.itail.ccs3000.core.Settings
import com.itail.ccs3000.core.SettingsInput

private val Blue = Color(0xFF0062C2)
private val Orange = Color(0xFFF4720A)

class MainActivity : ComponentActivity() {
    private val vm: MainViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme(colorScheme = lightColorScheme(primary = Blue, secondary = Orange)) {
                AppRoot(vm)
            }
        }
    }
}

@Composable
private fun AppRoot(vm: MainViewModel) {
    var tab by remember { mutableIntStateOf(0) }
    var showCamera by remember { mutableStateOf(false) }
    val focus = remember { FocusRequester() }

    Column(
        Modifier
            .fillMaxSize()
            .background(Color(0xFFF4F7FB))
            // F2 = back to the scan box, F5 = resend last, Esc = clear (same as the Windows version)
            .onPreviewKeyEvent { e ->
                if (e.type != KeyEventType.KeyDown) false
                else when (e.key) {
                    Key.Escape -> { if (tab == 0) { vm.clearScan(); true } else false }
                    Key.F2 -> { tab = 0; runCatching { focus.requestFocus() }; true }
                    Key.F5 -> { if (tab == 0) { vm.resendLast(); true } else false }
                    else -> false
                }
            },
    ) {
        Box(Modifier.fillMaxWidth().background(Blue).padding(horizontal = 20.dp, vertical = 14.dp)) {
            Column {
                Text("CCS3000 Ink Printer", color = Color.White, fontSize = 20.sp, fontWeight = FontWeight.Bold)
                Text("ส่งข้อมูลจาก QR / Barcode เข้า Text module ของเครื่องพิมพ์ KGK", color = Color(0xFFD6E6F8), fontSize = 13.sp)
            }
        }
        StatusBanner(vm.status)
        TabRow(selectedTabIndex = tab) {
            listOf("สแกน", "ตั้งค่า", "Log").forEachIndexed { i, t ->
                Tab(selected = tab == i, onClick = { tab = i }, text = { Text(t) })
            }
        }
        Box(Modifier.fillMaxSize()) {
            when (tab) {
                0 -> ScanTab(vm, focus) { showCamera = true }
                1 -> SettingsTab(vm)
                else -> LogTab(vm)
            }
        }
    }

    if (showCamera) {
        CameraScanDialog(
            initialFacing = vm.config.cameraFacing,
            onFacingChanged = vm::setCameraFacing,
            onScanned = { showCamera = false; tab = 0; vm.onCameraScanned(it) },
            onClose = { showCamera = false },
        )
    }

    vm.dialogError?.let { err ->
        AlertDialog(
            onDismissRequest = {},
            title = { Text(err.title) },
            text = { Text(err.message) },
            confirmButton = { TextButton(onClick = vm::dismissError) { Text("ตกลง") } },
        )
    }
}

@Composable
private fun StatusBanner(s: Status) {
    val (bg, fg, icon) = when (s.kind) {
        StatusKind.READY -> Triple(Color(0xFFE3F1FB), Color(0xFF004A96), "●")
        StatusKind.BUSY -> Triple(Color(0xFFFFF3D9), Color(0xFF8A4B00), "◌")
        StatusKind.OK -> Triple(Color(0xFFE0F6EA), Color(0xFF12713A), "✔")
        StatusKind.ERROR -> Triple(Color(0xFFFFE4E0), Color(0xFFA8201A), "✖")
    }
    Row(Modifier.fillMaxWidth().background(bg).padding(horizontal = 20.dp, vertical = 12.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(icon, color = fg, fontSize = 26.sp)
        Spacer(Modifier.padding(horizontal = 8.dp))
        Column {
            Text(s.title, color = fg, fontSize = 20.sp, fontWeight = FontWeight.Bold)
            if (s.detail.isNotEmpty()) Text(s.detail, color = fg, fontSize = 14.sp)
        }
    }
}

@Composable
private fun ScanTab(vm: MainViewModel, focus: FocusRequester, openCamera: () -> Unit) {
    val keyboard = LocalSoftwareKeyboardController.current
    val cfg = vm.config
    androidx.compose.runtime.LaunchedEffect(Unit) { runCatching { focus.requestFocus() } }
    androidx.compose.runtime.LaunchedEffect(cfg.hideSoftKeyboard) { if (cfg.hideSoftKeyboard) keyboard?.hide() }

    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Card(colors = CardDefaults.cardColors(containerColor = Color.White)) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text("ช่องรับข้อมูลจากเครื่องสแกน", fontWeight = FontWeight.Bold)
                OutlinedTextField(
                    value = vm.scanText,
                    onValueChange = vm::onScanChanged,
                    modifier = Modifier.fillMaxWidth().focusRequester(focus).onPreviewKeyEvent { e ->
                        // a USB / Bluetooth scanner ends with Enter
                        if (e.type == KeyEventType.KeyDown && (e.key == Key.Enter || e.key == Key.NumPadEnter)) { vm.submitScan(); true } else false
                    },
                    textStyle = androidx.compose.ui.text.TextStyle(fontFamily = FontFamily.Monospace, fontSize = 18.sp),
                    singleLine = true,
                    enabled = !vm.sending,
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                    keyboardActions = KeyboardActions(onSend = { vm.submitScan() }),
                    placeholder = { Text("แตะที่นี่แล้วสแกน (${cfg.expectedLength} ตัว)") },
                    supportingText = { Text("${vm.scanText.length} / ${cfg.expectedLength} ตัว") },
                )
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(onClick = openCamera, enabled = !vm.sending) { Text("สแกนด้วยกล้อง") }
                    OutlinedButton(onClick = vm::resendLast, enabled = vm.canResend && !vm.sending) { Text("ส่งซ้ำ (F5)") }
                    OutlinedButton(onClick = vm::clearScan, enabled = !vm.sending) { Text("ล้าง (Esc)") }
                }
                if (vm.sending) LinearProgressIndicator(Modifier.fillMaxWidth())
            }
        }

        // what the text read so far will look like once split
        val preview = com.itail.ccs3000.core.Payload.previewChunks(vm.scanText, cfg.fieldCount, cfg.digitsPerField)
        if (preview != null && vm.rows.isEmpty()) {
            Card(colors = CardDefaults.cardColors(containerColor = Color.White)) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text("ตัวอย่างการแบ่งข้อมูล", fontWeight = FontWeight.Bold)
                    cfg.moduleNumbers.zip(preview).forEach { (m, c) -> FieldLine(m, c) }
                }
            }
        }

        if (vm.rows.isNotEmpty()) {
            Card(colors = CardDefaults.cardColors(containerColor = Color.White)) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text("ผลการส่ง", fontWeight = FontWeight.Bold)
                    vm.rows.forEach { r ->
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Box(Modifier.widthIn(min = 28.dp)) {
                                when (r.ok) {
                                    null -> CircularProgressIndicator(Modifier.height(18.dp).widthIn(max = 18.dp), strokeWidth = 2.dp)
                                    true -> Text("✔", color = Color(0xFF12713A), fontWeight = FontWeight.Bold)
                                    false -> Text("✖", color = Color(0xFFA8201A), fontWeight = FontWeight.Bold)
                                }
                            }
                            Column(Modifier.weight(1f)) { FieldLine(r.module, r.text) }
                            Text(r.note, color = if (r.ok == false) Color(0xFFA8201A) else Color.Gray, fontSize = 13.sp)
                        }
                    }
                }
            }
        }
        Text(
            "ใช้เครื่องสแกน USB / Bluetooth ได้เลย (ทำงานเหมือนคีย์บอร์ด) หรือกด “สแกนด้วยกล้อง”",
            color = Color.Gray, fontSize = 13.sp,
        )
    }
}

@Composable
private fun FieldLine(module: Int, text: String) {
    // spaces are shown as a visible dot so trailing padding can be seen
    Text("M$module  |${text.replace(' ', '·')}|", fontFamily = FontFamily.Monospace, fontSize = 14.sp)
}

@Composable
private fun SettingsTab(vm: MainViewModel) {
    val c = vm.config
    var ip by remember(c) { mutableStateOf(c.printerIp) }
    var port by remember(c) { mutableStateOf(c.printerPort.toString()) }
    var digits by remember(c) { mutableStateOf(c.digitsPerField.toString()) }
    var msg by remember(c) { mutableStateOf(c.messageNo.toString()) }
    var modules by remember(c) { mutableStateOf(c.moduleNumbers.joinToString(",")) }
    var codeSystem by remember(c) { mutableStateOf(c.codeSystem) }
    var checksum by remember(c) { mutableStateOf(c.useChecksum) }
    var hideKb by remember(c) { mutableStateOf(c.hideSoftKeyboard) }
    var confirmTest by remember { mutableStateOf(false) }

    fun form() = SettingsInput(ip, port, digits, msg, codeSystem, modules, checksum, hideKb)

    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Card(colors = CardDefaults.cardColors(containerColor = Color.White)) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("เครื่องพิมพ์", fontWeight = FontWeight.Bold)
                OutlinedTextField(ip, { ip = it }, label = { Text("Printer IP") }, singleLine = true, modifier = Modifier.fillMaxWidth(),
                    keyboardOptions = KeyboardOptions(keyboardType = androidx.compose.ui.text.input.KeyboardType.Uri))
                OutlinedTextField(port, { port = it }, label = { Text("Port") }, singleLine = true, modifier = Modifier.fillMaxWidth(),
                    keyboardOptions = KeyboardOptions(keyboardType = androidx.compose.ui.text.input.KeyboardType.Number))
                OutlinedTextField(modules, { modules = it }, label = { Text("หมายเลข Text module (คั่นด้วย ,)") }, singleLine = true, modifier = Modifier.fillMaxWidth())
                OutlinedTextField(digits, { digits = it }, label = { Text("ตัวอักษรต่อ field") }, singleLine = true, modifier = Modifier.fillMaxWidth(),
                    keyboardOptions = KeyboardOptions(keyboardType = androidx.compose.ui.text.input.KeyboardType.Number))
                OutlinedTextField(msg, { msg = it }, label = { Text("Message no. (0-999)") }, singleLine = true, modifier = Modifier.fillMaxWidth(),
                    keyboardOptions = KeyboardOptions(keyboardType = androidx.compose.ui.text.input.KeyboardType.Number))
                Text("Code system")
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Settings.CODE_SYSTEMS.forEach { cs ->
                        FilterChip(selected = codeSystem == cs, onClick = { codeSystem = cs }, label = { Text(cs) })
                    }
                }
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Switch(checksum, { checksum = it })
                    Spacer(Modifier.padding(horizontal = 6.dp))
                    Text("ใช้ Checksum")
                }
            }
        }
        Card(colors = CardDefaults.cardColors(containerColor = Color.White)) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("แท็บเล็ต", fontWeight = FontWeight.Bold)
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Switch(hideKb, { hideKb = it })
                    Spacer(Modifier.padding(horizontal = 6.dp))
                    Text("ซ่อนคีย์บอร์ดบนจอเมื่อรอเครื่องสแกน (USB/Bluetooth)")
                }
            }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = { vm.saveSettings(form()) }, enabled = !vm.testing) { Text("บันทึก") }
            OutlinedButton(onClick = { confirmTest = true }, enabled = !vm.testing && !vm.sending) {
                Text(if (vm.testing) "กำลังทดสอบ..." else "ทดสอบการเชื่อมต่อ")
            }
        }
        if (vm.testing) LinearProgressIndicator(Modifier.fillMaxWidth())
    }

    if (confirmTest) {
        AlertDialog(
            onDismissRequest = {},
            title = { Text("ทดสอบการเชื่อมต่อ") },
            text = { Text("ระบบจะส่งข้อความ “TEST M<เลข module>” เข้า Text module ทุกตัว (${modules}) — ข้อความที่ค้างอยู่ในเครื่องจะถูกเขียนทับ ต้องการทดสอบต่อหรือไม่?") },
            confirmButton = { TextButton(onClick = { confirmTest = false; vm.testConnection(form()) }) { Text("ทดสอบ") } },
            dismissButton = { TextButton(onClick = { confirmTest = false }) { Text("ยกเลิก") } },
        )
    }
}

@Composable
private fun LogTab(vm: MainViewModel) {
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
        if (vm.log.isEmpty()) Text("ยังไม่มีรายการ", color = Color.Gray)
        vm.log.forEach { Text(it, fontFamily = FontFamily.Monospace, fontSize = 13.sp) }
    }
}
