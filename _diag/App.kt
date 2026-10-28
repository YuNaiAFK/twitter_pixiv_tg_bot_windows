package com.zt.volte5g

import android.app.Application
import com.google.android.material.color.DynamicColors

/**
 * Material You 动态取色：Android 12+ 下让控件配色跟随系统壁纸，
 * 与系统设置等原生界面视觉一致（系统不支持时自动回退静态主题）。
 */
class App : Application() {
    override fun onCreate() {
        super.onCreate()
        DynamicColors.applyToActivitiesIfAvailable(this)
    }
}
