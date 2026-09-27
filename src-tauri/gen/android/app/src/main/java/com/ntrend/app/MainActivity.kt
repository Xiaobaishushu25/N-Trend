package com.ntrend.app

import android.content.pm.ActivityInfo
import android.graphics.Color
import android.os.Bundle
import android.webkit.JavascriptInterface
import android.webkit.WebView
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat

class MainActivity : TauriActivity() {
  override fun onCreate(savedInstanceState: Bundle?) {
    super.onCreate(savedInstanceState)
    window.decorView.setBackgroundColor(Color.parseColor("#14171d"))
    val insetsController = WindowCompat.getInsetsController(window, window.decorView)
    insetsController.isAppearanceLightStatusBars = true
    insetsController.isAppearanceLightNavigationBars = true

    val root = window.decorView.findViewById<android.view.View>(android.R.id.content) ?: window.decorView
    ViewCompat.setOnApplyWindowInsetsListener(root) { view, insets ->
      val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
      view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
      insets
    }
  }

  override fun onWebViewCreate(webView: WebView) {
    super.onWebViewCreate(webView)
    webView.addJavascriptInterface(object {
      @JavascriptInterface
      fun setOrientation(orientation: String) {
        runOnUiThread {
          val insetsController = WindowCompat.getInsetsController(window, window.decorView)
          when (orientation) {
            "landscape" -> {
              requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
              insetsController.systemBarsBehavior = WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
              insetsController.hide(WindowInsetsCompat.Type.statusBars())
            }
            "portrait" -> {
              requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_PORTRAIT
              insetsController.show(WindowInsetsCompat.Type.statusBars())
            }
            else -> {
              requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED
              insetsController.show(WindowInsetsCompat.Type.statusBars())
            }
          }
        }
      }
    }, "AndroidBridge")
  }
}
