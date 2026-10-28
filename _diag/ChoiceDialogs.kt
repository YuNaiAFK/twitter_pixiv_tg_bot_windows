package com.zt.volte5g

import android.content.Context
import android.util.AttributeSet
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.ArrayAdapter
import android.widget.Checkable
import android.widget.LinearLayout
import android.widget.TextView
import androidx.core.view.isVisible

/** 对话框单选行数据：主标题 + 可选副标题（与系统设置的选项列表同构） */
data class ChoiceItem(val title: String, val subtitle: String? = null, val value: Int)

/** 两行单选列表适配器；选中态由 [CheckableRowLayout] 委托给行内单选圆点渲染 */
class ChoiceAdapter(
    context: Context,
    private val items: List<ChoiceItem>,
    private val checkedPosition: Int,
) : ArrayAdapter<ChoiceItem>(context, 0, items) {

    override fun getView(position: Int, convertView: View?, parent: ViewGroup): View {
        val view = convertView ?: LayoutInflater.from(context)
            .inflate(R.layout.dialog_choice_item, parent, false)
        val item = items[position]
        view.findViewById<TextView>(R.id.title).text = item.title
        val subtitle = view.findViewById<TextView>(R.id.subtitle)
        subtitle.isVisible = !item.subtitle.isNullOrBlank()
        subtitle.text = item.subtitle.orEmpty()
        (view as CheckableRowLayout).isChecked = position == checkedPosition
        return view
    }
}

/**
 * 可选中行容器：AlertDialog 的单选列表要求行根视图实现 [Checkable]，
 * 这里把选中态委托给行内的 MaterialRadioButton，获得与系统设置一致的单选视觉。
 */
class CheckableRowLayout @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyleAttr: Int = 0,
) : LinearLayout(context, attrs, defStyleAttr), Checkable {

    private var checkedState = false
    private var radio: Checkable? = null

    override fun onFinishInflate() {
        super.onFinishInflate()
        radio = findViewById(R.id.radio)
    }

    override fun setChecked(checked: Boolean) {
        checkedState = checked
        (radio ?: findViewById<View>(R.id.radio) as? Checkable)?.isChecked = checked
    }

    override fun isChecked(): Boolean = checkedState

    override fun toggle() {
        isChecked = !checkedState
    }
}
