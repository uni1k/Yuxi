<template>
  <a-dropdown trigger="click" @open-change="handleOpenChange">
    <div class="model-select" @click.prevent>
      <div class="model-select-content">
        <div class="model-info">
          <a-tooltip :title="tooltipTitle" placement="right">
            <span class="model-text">{{ displayText }}</span>
          </a-tooltip>
        </div>
      </div>
    </div>

    <template #overlay>
      <a-menu class="scrollable-menu">
        <a-menu-item-group v-for="(providerData, providerId) in v2Models" :key="providerId">
          <template #title>
            <span>{{ providerId }}</span>
          </template>
          <a-menu-item
            v-for="model in providerData.models"
            :key="model.spec"
            @click="handleSelect(model.spec)"
          >
            {{ model.display_name }}
          </a-menu-item>
        </a-menu-item-group>
      </a-menu>
    </template>
  </a-dropdown>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { modelProviderApi } from '@/apis/system_api'

const props = defineProps({
  value: {
    type: String,
    default: ''
  },
  placeholder: {
    type: String,
    default: '请选择重排序模型'
  },
  size: {
    type: String,
    default: 'small',
    validator: (value) => ['small', 'middle', 'large'].includes(value)
  },
  style: {
    type: Object,
    default: () => ({ width: '100%' })
  },
  disabled: {
    type: Boolean,
    default: false
  }
})

const emit = defineEmits(['update:value', 'change'])

const v2Models = ref({})

const findModelBySpec = (spec) => {
  if (!spec) return null
  for (const providerData of Object.values(v2Models.value)) {
    const found = providerData.models?.find((m) => m.spec === spec)
    if (found) return found
  }
  return null
}

const displayText = computed(() => {
  if (!props.value) return props.placeholder
  const model = findModelBySpec(props.value)
  if (model?.display_name) return model.display_name
  return props.value
})

const tooltipTitle = computed(() => {
  if (!props.value) return props.placeholder
  const model = findModelBySpec(props.value)
  if (model?.display_name && model.display_name !== props.value) {
    return `${model.display_name} (${props.value})`
  }
  return props.value
})

let fetchV2ModelsPromise = null

const fetchV2Models = async () => {
  if (fetchV2ModelsPromise) return fetchV2ModelsPromise

  fetchV2ModelsPromise = (async () => {
    try {
      const response = await modelProviderApi.getV2Models('rerank')
      if (response.success) {
        v2Models.value = response.data || {}
      }
    } catch (error) {
      console.error('获取 rerank 模型失败:', error)
    } finally {
      fetchV2ModelsPromise = null
    }
  })()

  return fetchV2ModelsPromise
}

const handleOpenChange = async (open) => {
  if (!open) return
  await fetchV2Models()
}

// 已选模型已知时后台静默拉取，避免必须点开下拉才能看到 display_name
watch(
  () => props.value,
  (value) => {
    if (value && Object.keys(v2Models.value).length === 0 && !fetchV2ModelsPromise) {
      fetchV2Models()
    }
  },
  { immediate: true }
)

const handleSelect = (spec) => {
  emit('update:value', spec)
  emit('change', spec)
}
</script>

<style lang="less" scoped>
@import '@/assets/css/model-selector-common.less';
</style>
