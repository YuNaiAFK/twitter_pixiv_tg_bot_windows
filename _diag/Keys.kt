package com.zt.volte5g

/**
 * 以字面量引用的 CarrierConfig 键名与 IMS 常量。
 * 这些常量多数为 @hide，直接引用会导致编译失败，故此处用字符串/字面量。
 * 键名与 AOSP android.telephony.CarrierConfigManager 中的定义一致。
 */
object Keys {
    // ---- IMS provisioning（android.telephony.ims.ProvisioningManager，@hide）----
    const val KEY_VOIMS_OPT_IN_STATUS = 68
    const val PROVISIONING_VALUE_ENABLED = 1

    // ---- 版本指纹键：用于判断覆写是否已经是当前版本写入 ----
    const val KEY_CONFIG_VERSION = "pv5g_config_version"

    // ---- CarrierConfig 键名 ----
    const val SHOW_IMS_REGISTRATION_STATUS = "show_ims_registration_status_bool"
    const val CARRIER_VOLTE_AVAILABLE = "carrier_volte_available_bool"
    const val EDITABLE_ENHANCED_4G_LTE = "editable_enhanced_4g_lte_bool"
    const val HIDE_ENHANCED_4G_LTE = "hide_enhanced_4g_lte_bool"
    const val HIDE_LTE_PLUS_ICON = "hide_lte_plus_data_icon_bool"

    const val CARRIER_WFC_IMS_AVAILABLE = "carrier_wfc_ims_available_bool"
    const val CARRIER_WFC_SUPPORTS_WIFI_ONLY = "carrier_wfc_supports_wifi_only_bool"
    const val EDITABLE_WFC_MODE = "editable_wfc_mode_bool"
    const val EDITABLE_WFC_ROAMING_MODE = "editable_wfc_roaming_mode_bool"
    const val SHOW_WFC_ICON = "show_wifi_calling_icon_in_status_bar_bool"
    const val WFC_SPN_FORMAT_IDX = "wfc_spn_format_idx_int"

    const val CARRIER_VT_AVAILABLE = "carrier_vt_available_bool"
    const val CARRIER_SS_OVER_UT = "carrier_supports_ss_over_ut_bool"

    const val CROSS_SIM_IMS_AVAILABLE = "carrier_cross_sim_ims_available_bool"
    const val CROSS_SIM_ON_OPPORTUNISTIC = "enable_cross_sim_calling_on_opportunistic_data_bool"

    const val VONR_ENABLED = "vonr_enabled_bool"
    const val VONR_SETTING_VISIBILITY = "vonr_setting_visibility_bool"

    const val CARRIER_NR_AVAILABILITIES = "carrier_nr_availabilities_int_array"
    const val NR_AVAILABILITY_NSA = 1
    const val NR_AVAILABILITY_SA = 2
    const val NR_SSRSRP_THRESHOLDS = "5g_nr_ssrsrp_thresholds_int_array"
}

/** SharedPreferences 名与键 */
object Prefs {
    const val NAME = "volte5g_config"
    const val KEY_SELECTED_SUB = "selected_subid"

    const val VOLTE = "volte"
    const val VONR = "vonr"
    const val NR5G = "5g_nr"
    const val VOWIFI = "vowifi"
    const val VT = "vt"
    const val CROSS_SIM = "cross_sim"
    const val UT = "ut"

    /** 5G 组网模式：部分运营商在 NSA+SA 组合下 NR 不注册，需仅 SA（Pixel IMS issue #423） */
    const val NR_MODE = "nr_mode"
    const val NR_MODE_BOTH = 0
    const val NR_MODE_SA = 1
    const val NR_MODE_NSA = 2
}
