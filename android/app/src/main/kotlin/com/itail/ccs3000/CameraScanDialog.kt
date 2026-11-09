package com.itail.ccs3000

import android.Manifest
import android.content.pm.PackageManager
import android.util.Size
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.compose.ui.window.Dialog
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.LocalLifecycleOwner
import com.google.zxing.BarcodeFormat
import com.google.zxing.BinaryBitmap
import com.google.zxing.DecodeHintType
import com.google.zxing.MultiFormatReader
import com.google.zxing.PlanarYUVLuminanceSource
import com.google.zxing.common.HybridBinarizer
import java.util.EnumMap
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/** decodes QR / barcodes from CameraX frames with ZXing (no Google Play services needed) */
private class ZxingAnalyzer(private val onResult: (String) -> Unit) : ImageAnalysis.Analyzer {
    private val reader = MultiFormatReader().apply {
        val hints = EnumMap<DecodeHintType, Any>(DecodeHintType::class.java)
        hints[DecodeHintType.POSSIBLE_FORMATS] = listOf(
            BarcodeFormat.QR_CODE, BarcodeFormat.DATA_MATRIX, BarcodeFormat.CODE_128, BarcodeFormat.CODE_39,
            BarcodeFormat.CODE_93, BarcodeFormat.EAN_13, BarcodeFormat.EAN_8, BarcodeFormat.UPC_A,
            BarcodeFormat.UPC_E, BarcodeFormat.ITF, BarcodeFormat.CODABAR, BarcodeFormat.PDF_417, BarcodeFormat.AZTEC,
        )
        hints[DecodeHintType.TRY_HARDER] = true
        setHints(hints)
    }
    private val done = AtomicBoolean(false)

    override fun analyze(image: ImageProxy) {
        try {
            if (done.get()) return
            val plane = image.planes[0]
            val w = image.width
            val h = image.height
            val buf = plane.buffer
            val rowStride = plane.rowStride
            // copy the Y (luminance) plane without the row padding
            var data = ByteArray(w * h)
            val row = ByteArray(rowStride)
            for (y in 0 until h) {
                buf.position(y * rowStride)
                val n = minOf(rowStride, buf.remaining())
                buf.get(row, 0, n)
                System.arraycopy(row, 0, data, y * w, w)
            }
            var ww = w
            var hh = h
            // bring the picture upright so 1-D barcodes are read along their lines
            when (image.imageInfo.rotationDegrees) {
                90 -> { data = rotate90(data, ww, hh); ww = h; hh = w }
                180 -> data = rotate180(data)
                270 -> { data = rotate90(rotate180(data), ww, hh); ww = h; hh = w }
            }
            val source = PlanarYUVLuminanceSource(data, ww, hh, 0, 0, ww, hh, false)
            try {
                val result = reader.decodeWithState(BinaryBitmap(HybridBinarizer(source)))
                val text = result.text
                if (!text.isNullOrEmpty() && done.compareAndSet(false, true)) onResult(text)
            } catch (_: Exception) {
                // nothing readable in this frame
            } finally {
                reader.reset()
            }
        } finally {
            image.close()
        }
    }

    private fun rotate90(src: ByteArray, w: Int, h: Int): ByteArray {
        val out = ByteArray(src.size)
        for (y in 0 until h) for (x in 0 until w) out[x * h + (h - 1 - y)] = src[y * w + x]
        return out
    }

    private fun rotate180(src: ByteArray): ByteArray {
        val out = ByteArray(src.size)
        for (i in src.indices) out[src.size - 1 - i] = src[i]
        return out
    }
}

@Composable
fun CameraScanDialog(
    initialFacing: String,
    onFacingChanged: (String) -> Unit,
    onScanned: (String) -> Unit,
    onClose: () -> Unit,
) {
    val context = LocalContext.current
    var granted by remember {
        mutableStateOf(ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED)
    }
    var asked by remember { mutableStateOf(false) }
    val launcher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted = it; asked = true }
    LaunchedEffect(Unit) { if (!granted) launcher.launch(Manifest.permission.CAMERA) }

    Dialog(onDismissRequest = {}) {
        androidx.compose.material3.Surface(shape = androidx.compose.foundation.shape.RoundedCornerShape(16.dp)) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text("สแกนด้วยกล้อง", style = androidx.compose.material3.MaterialTheme.typography.titleMedium)
                if (granted) {
                    var facing by remember { mutableStateOf(initialFacing) }
                    CameraPreview(facing = facing, onScanned = onScanned)
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        OutlinedButton(onClick = {
                            facing = if (facing == "back") "front" else "back"
                            onFacingChanged(facing)
                        }) { Text(if (facing == "back") "สลับเป็นกล้องหน้า" else "สลับเป็นกล้องหลัง") }
                    }
                } else {
                    Text(
                        if (asked) "ไม่ได้รับสิทธิ์ใช้กล้อง — เปิดสิทธิ์ในตั้งค่าแอป หรือใช้เครื่องสแกน USB/Bluetooth แทน"
                        else "กำลังขอสิทธิ์ใช้กล้อง...",
                    )
                }
                Button(onClick = onClose, modifier = Modifier.fillMaxWidth()) { Text("ปิด") }
            }
        }
    }
}

@Composable
private fun CameraPreview(facing: String, onScanned: (String) -> Unit) {
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current
    val previewView = remember { PreviewView(context).apply { scaleType = PreviewView.ScaleType.FILL_CENTER } }
    val executor = remember { Executors.newSingleThreadExecutor() }
    val latestOnScanned by androidx.compose.runtime.rememberUpdatedState(onScanned)
    val mainExecutor = remember { ContextCompat.getMainExecutor(context) }

    DisposableEffect(facing) {
        val future = ProcessCameraProvider.getInstance(context)
        var provider: ProcessCameraProvider? = null
        future.addListener({
            val p = future.get()
            provider = p
            val preview = Preview.Builder().build().also { it.setSurfaceProvider(previewView.surfaceProvider) }
            @Suppress("DEPRECATION")
            val analysis = ImageAnalysis.Builder()
                .setTargetResolution(Size(1280, 720))
                .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                .build()
                .also { a -> a.setAnalyzer(executor, ZxingAnalyzer { text -> mainExecutor.execute { latestOnScanned(text) } }) }
            val wanted = if (facing == "front") CameraSelector.DEFAULT_FRONT_CAMERA else CameraSelector.DEFAULT_BACK_CAMERA
            val selector = if (p.hasCamera(wanted)) wanted
            else if (p.hasCamera(CameraSelector.DEFAULT_BACK_CAMERA)) CameraSelector.DEFAULT_BACK_CAMERA
            else CameraSelector.DEFAULT_FRONT_CAMERA
            try {
                p.unbindAll()
                p.bindToLifecycle(lifecycleOwner, selector, preview, analysis)
            } catch (e: Exception) {
                android.util.Log.e("CameraScan", "bind failed", e)
            }
        }, mainExecutor)
        onDispose { provider?.unbindAll() }
    }
    DisposableEffect(Unit) { onDispose { executor.shutdown() } }

    AndroidView(factory = { previewView }, modifier = Modifier.fillMaxWidth().height(320.dp))
}
