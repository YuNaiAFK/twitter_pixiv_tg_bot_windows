package com.zt.volte5g

import android.Manifest
import android.content.Intent
import android.content.SharedPreferences
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.telephony.SubscriptionManager
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.core.view.isVisible
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.zt.volte5g.databinding.ActivityMainBinding
import rikka.shizuku.Shizuku

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var prefs: SharedPreferences
    private lateinit var profile: DeviceProfile

    private val binderReceivedListener = Shizuku.OnBinderReceivedListener {
        runOnUiThread { updateShizukuStatus() }
    }
    private val binderDeadListener = Shizuku.OnBinderDeadListener {
        runOnUiThread { updateShizukuStatus() }
    }
    private val permResultListener = Shizuku.OnRequestPermissionResultListener { _, _ ->
        runOnUiThread { updateShizukuStatus() }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        prefs = getSharedPreferences(Prefs.NAME, MODE_PRIVATE)
        profile = DeviceSupport.detect(this)

        requestReadPhoneStateIfNeeded()
        bindDeviceCard()
        loadSwitches()
        bindListeners()
        updateSimButtonLabel()

        Shizuku.addBinderReceivedListenerSticky(binderReceivedListener)
        Shizuku.addBinderDeadListener(binderDeadListener)
        Shizuku.addRequestPermissionResultListener(permResultListener)

        updateShizukuStatus()
        updateStatusInfo()
    }

    override fun onResume() {
        super.onResume()
        updateShizukuStatus()
        updateStatusInfo()
        updateSimButtonLabel()
    }

    override fun onDestroy() {
        super.onDestroy()
        Shizuku.removeBinderReceivedListener(binderReceivedListener)
        Shizuku.removeBinderDeadListener(binderDeadListener)
        Shizuku.removeRequestPermissionResultListener(permResultListener)
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<out String>, grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQ_READ_PHONE_STATE) {
            updateStatusInfo()
            updateSimButtonLabel()
        }
    }

    private fun requestReadPhoneStateIfNeeded() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.READ_PHONE_STATE)
            != PackageManager.PERMISSION_GRANTED
        ) {
            ActivityCompat.requestPermissions(
                this, arrayOf(Manifest.permission.READ_PHONE_STATE), REQ_READ_PHONE_STATE
            )
        }
    }

    private fun bindDeviceCard() {
        binding.tvDeviceName.text = if (profile.isPixel) {
            getString(R.string.device_name_fmt, profile.name, profile.device)
        } else {
            profile.name
        }
        binding.tvDeviceAndroid.text = getString(
            R.string.android_version_fmt, Build.VERSION.RELEASE, Build.VERSION.SDK_INT
        )
        binding.chipDeviceBadge.text = getString(
            if (profile.isPixel) R.string.badge_pixel else R.string.badge_not_pixel
        )
        binding.chip5gBadge.text = getString(
            when {
                !profile.telephony -> R.string.badge_no_telephony
                !profile.has5G -> R.string.badge_no_5g
                profile.hasSa -> R.string.badge_5g_nsa_sa
                else -> R.string.badge_5g_nsa
            }
        )
        when {
            !profile.telephony -> {
                binding.tvDeviceNote.setText(R.string.unsupported_no_telephony)
                binding.tvDeviceNote.isVisible = true
            }
            profile.isPixel && !profile.has5G -> {
                binding.tvDeviceNote.setText(R.string.unsupported_no_5g)
                binding.tvDeviceNote.isVisible = true
            }
            !profile.isPixel -> {
                binding.tvDeviceNote.setText(R.string.untested_device)
                binding.tvDeviceNote.isVisible = true
            }
            else -> binding.tvDeviceNote.isVisible = false
        }
        // 无 5G 能力的机型禁用 5G / VoNR 开关
        binding.sw5gNr.isEnabled = profile.has5G
        binding.swVonr.isEnabled = profile.hasVoNR
        binding.btnNrMode.isVisible = profile.has5G && binding.sw5gNr.isChecked
    }

    private fun loadSwitches() {
        binding.swVolte.isChecked = prefs.getBoolean(Prefs.VOLTE, true)
        binding.swVonr.isChecked = prefs.getBoolean(Prefs.VONR, true) && profile.hasVoNR
        binding.sw5gNr.isChecked = prefs.getBoolean(Prefs.NR5G, true) && profile.has5G
        binding.swVowifi.isChecked = prefs.getBoolean(Prefs.VOWIFI, true)
        binding.swVt.isChecked = prefs.getBoolean(Prefs.VT, true)
        binding.swCrossSim.isChecked = prefs.getBoolean(Prefs.CROSS_SIM, true)
        binding.swUt.isChecked = prefs.getBoolean(Prefs.UT, true)
        updateNrModeLabel()
    }

    private fun bindListeners() {
        listOf(
            binding.swVolte to Prefs.VOLTE,
            binding.swVonr to Prefs.VONR,
            binding.sw5gNr to Prefs.NR5G,
            binding.swVowifi to Prefs.VOWIFI,
            binding.swVt to Prefs.VT,
            binding.swCrossSim to Prefs.CROSS_SIM,
            binding.swUt to Prefs.UT,
        ).forEach { (switch, key) ->
            switch.setOnCheckedChangeListener { _, checked ->
                prefs.edit().putBoolean(key, checked).apply()
                if (switch === binding.sw5gNr) update5gModeVisibility()
            }
        }

        binding.btnApply.setOnClickListener { applyConfiguration() }
        binding.btnSelectSim.setOnClickListener { showSimSelection() }
        binding.btnOpenShizuku.setOnClickListener { openShizukuApp() }
        binding.btnNrMode.setOnClickListener { showNrModeSelection() }
    }

    private fun update5gModeVisibility() {
        binding.btnNrMode.isVisible = profile.has5G && binding.sw5gNr.isChecked
    }

    private fun updateNrModeLabel() {
        binding.btnNrMode.text = when (prefs.getInt(Prefs.NR_MODE, Prefs.NR_MODE_BOTH)) {
            Prefs.NR_MODE_SA -> getString(R.string.nr_mode_sa)
            Prefs.NR_MODE_NSA -> getString(R.string.nr_mode_nsa)
            else -> getString(R.string.nr_mode_both)
        }
    }

    // -------------------------------------------------------------------------
    // SIM 选择（系统设置同款双行单选列表：运营商名 + 卡槽标识，只列实际插卡的卡槽）
    // -------------------------------------------------------------------------

    private fun updateSimButtonLabel() {
        binding.btnSelectSim.text = getString(R.string.sim_target_fmt, currentSimLabel())
    }

    private fun currentSimLabel(): String = when (prefs.getInt(Prefs.KEY_SELECTED_SUB, -1)) {
        1 -> slotLabel(1, carrierNameForSlot(0))
        2 -> slotLabel(2, carrierNameForSlot(1))
        else -> getString(R.string.all_sims)
    }

    private fun slotLabel(slotNumber: Int, carrier: String?): String =
        if (carrier.isNullOrBlank()) {
            getString(R.string.sim_slot_plain_fmt, slotNumber)
        } else {
            getString(R.string.sim_slot_fmt, slotNumber, carrier)
        }

    /** 读取各卡槽（槽序号 0 起）的运营商名；无权限或无 SIM 时返回 null */
    private fun activeSimInfos(): List<Pair<Int, String?>>? = runCatching {
        val sm = getSystemService(SubscriptionManager::class.java) ?: return null
        sm.activeSubscriptionInfoList?.map { info ->
            info.simSlotIndex to
                (info.displayName ?: info.carrierName)?.toString()?.takeIf { it.isNotBlank() }
        }
    }.getOrNull()

    private fun carrierNameForSlot(slotIndex: Int): String? =
        activeSimInfos()?.firstOrNull { it.first == slotIndex }?.second

    /** 构造 SIM 选项：运营商名为标题、卡槽标识为副标题；读不到时只显示卡槽 */
    private fun buildSimOptions(): List<ChoiceItem> {
        val infos = activeSimInfos()
        val options = mutableListOf<ChoiceItem>()
        val slot1 = infos?.firstOrNull { it.first == 0 }
        val slot2 = infos?.firstOrNull { it.first == 1 }
        // 能读到订阅信息时只列出插了卡的卡槽；读不到时两个卡槽都列出
        if (infos == null || slot1 != null) {
            options += ChoiceItem(
                title = slot1?.second ?: getString(R.string.sim_slot_plain_fmt, 1),
                subtitle = if (slot1?.second != null) getString(R.string.sim_slot_plain_fmt, 1) else null,
                value = 1,
            )
        }
        if (infos == null || slot2 != null) {
            options += ChoiceItem(
                title = slot2?.second ?: getString(R.string.sim_slot_plain_fmt, 2),
                subtitle = if (slot2?.second != null) getString(R.string.sim_slot_plain_fmt, 2) else null,
                value = 2,
            )
        }
        options += ChoiceItem(
            title = getString(R.string.all_sims),
            subtitle = getString(R.string.all_sims_desc),
            value = -1,
        )
        return options
    }

    private fun showSimSelection() {
        val items = buildSimOptions()
        val current = prefs.getInt(Prefs.KEY_SELECTED_SUB, -1)
        val checked = items.indexOfFirst { it.value == current }.coerceAtLeast(0)
        val adapter = ChoiceAdapter(this, items, checked)

        MaterialAlertDialogBuilder(this)
            .setTitle(R.string.select_sim)
            .setSingleChoiceItems(adapter, checked) { dialog, which ->
                prefs.edit().putInt(Prefs.KEY_SELECTED_SUB, items[which].value).apply()
                updateSimButtonLabel()
                dialog.dismiss()
            }
            .setNegativeButton(R.string.cancel, null)
            .show()
    }

    private fun showNrModeSelection() {
        val current = prefs.getInt(Prefs.NR_MODE, Prefs.NR_MODE_BOTH)
        val items = listOf(
            ChoiceItem(
                getString(R.string.nr_mode_both),
                getString(R.string.nr_mode_both_desc),
                Prefs.NR_MODE_BOTH,
            ),
            ChoiceItem(
                getString(R.string.nr_mode_sa),
                getString(R.string.nr_mode_sa_desc),
                Prefs.NR_MODE_SA,
            ),
            ChoiceItem(
                getString(R.string.nr_mode_nsa),
                getString(R.string.nr_mode_nsa_desc),
                Prefs.NR_MODE_NSA,
            ),
        )
        val checked = items.indexOfFirst { it.value == current }.coerceAtLeast(0)
        val adapter = ChoiceAdapter(this, items, checked)

        MaterialAlertDialogBuilder(this)
            .setTitle(R.string.nr_mode_dialog_title)
            .setSingleChoiceItems(adapter, checked) { dialog, which ->
                prefs.edit().putInt(Prefs.NR_MODE, items[which].value).apply()
                updateNrModeLabel()
                dialog.dismiss()
            }
            .setNegativeButton(R.string.cancel, null)
            .show()
    }

    private fun shizukuAlive(): Boolean =
        runCatching { Shizuku.pingBinder() }.getOrDefault(false)

    private fun shizukuGranted(): Boolean =
        runCatching { Shizuku.checkSelfPermission() }
            .getOrDefault(PackageManager.PERMISSION_DENIED) == PackageManager.PERMISSION_GRANTED

    private fun updateShizukuStatus() {
        val (textRes, color, ready) = when {
            !shizukuAlive() -> Triple(R.string.shizuku_not_running, 0xFFF44336.toInt(), false)
            !shizukuGranted() -> Triple(R.string.shizuku_no_permission, 0xFFFF9800.toInt(), false)
            else -> Triple(R.string.shizuku_ready, 0xFF4CAF50.toInt(), true)
        }
        binding.chipShizuku.text = getString(R.string.shizuku_status_fmt, getString(textRes))
        binding.chipShizuku.setTextColor(color)
        binding.btnApply.isEnabled = ready && profile.telephony

        if (shizukuAlive() && !shizukuGranted()) requestShizukuPermission()
    }

    private fun requestShizukuPermission() {
        if (runCatching { Shizuku.isPreV11() }.getOrDefault(true)) {
            Toast.makeText(this, R.string.update_shizuku, Toast.LENGTH_LONG).show()
            return
        }
        runCatching { Shizuku.requestPermission(REQ_SHIZUKU) }
    }

    private fun openShizukuApp() {
        val intent = packageManager.getLaunchIntentForPackage("moe.shizuku.privileged.api")
        if (intent != null) {
            startActivity(intent)
        } else {
            Toast.makeText(this, R.string.shizuku_not_installed, Toast.LENGTH_LONG).show()
        }
    }

    private fun updateStatusInfo() {
        val persistent = runCatching { ShizukuProvider.canPersistent(this) }.getOrDefault(false)
        binding.chipPersistent.text = getString(
            R.string.persistent_mode_fmt,
            getString(if (persistent) R.string.yes else R.string.no)
        )
        binding.chipPersistent.setTextColor(if (persistent) COLOR_OK else COLOR_WARN)

        val applied = !runCatching { ShizukuProvider.needOverride(this) }.getOrDefault(true)
        binding.chipConfig.text = getString(
            R.string.config_status_fmt,
            getString(if (applied) R.string.applied else R.string.not_applied)
        )
        binding.chipConfig.setTextColor(if (applied) COLOR_OK else COLOR_IDLE)
    }

    private fun applyConfiguration() {
        if (!profile.telephony) {
            Toast.makeText(this, R.string.unsupported_no_telephony, Toast.LENGTH_LONG).show()
            return
        }
        if (!shizukuAlive()) {
            Toast.makeText(this, R.string.shizuku_not_running_msg, Toast.LENGTH_LONG).show()
            return
        }
        if (!shizukuGranted()) {
            requestShizukuPermission()
            Toast.makeText(this, R.string.shizuku_no_permission_msg, Toast.LENGTH_LONG).show()
            return
        }

        ShizukuProvider.applyNow(this)
        Toast.makeText(this, R.string.apply_started, Toast.LENGTH_SHORT).show()
        binding.btnApply.isEnabled = false
        binding.btnApply.setText(R.string.applying)
        binding.progressApply.isVisible = true

        // Instrumentation 会让进程短暂切到后台，3 秒后拉回界面并刷新状态
        Thread {
            try {
                Thread.sleep(3000)
            } catch (_: InterruptedException) {
            }
            runOnUiThread {
                binding.btnApply.setText(R.string.btn_apply)
                binding.progressApply.isVisible = false
                updateShizukuStatus()
                updateStatusInfo()
                startActivity(
                    Intent(this, MainActivity::class.java)
                        .setFlags(Intent.FLAG_ACTIVITY_REORDER_TO_FRONT)
                )
                MaterialAlertDialogBuilder(this)
                    .setTitle(R.string.apply_success_title)
                    .setMessage(R.string.apply_success_msg)
                    .setPositiveButton(R.string.goto_network_settings) { _, _ ->
                        runCatching { startActivity(Intent(Settings.ACTION_WIRELESS_SETTINGS)) }
                    }
                    .setNegativeButton(R.string.later, null)
                    .show()
            }
        }.start()
    }

    companion object {
        private const val REQ_SHIZUKU = 1001
        private const val REQ_READ_PHONE_STATE = 2001
        private const val COLOR_OK = 0xFF4CAF50.toInt()
        private const val COLOR_WARN = 0xFFF57C00.toInt()
        private const val COLOR_IDLE = 0xFF9E9E9E.toInt()
    }
}
