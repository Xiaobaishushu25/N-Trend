<script setup lang="ts">
import { onBeforeUnmount, watch } from 'vue'
import type { ReviewSummary } from '../types/review'
import { useReviewDetail } from '../utils/useReviewDetail'
import ReviewSignalDetailDrawer from './ReviewSignalDetailDrawer.vue'
const props = defineProps<{ row: ReviewSummary }>()
const { detail, loading, error, load, cancel } = useReviewDetail()
const reload = () => load(props.row.event_id, props.row.result_version)
watch(() => [props.row.event_id, props.row.result_version], reload, { immediate: true })
onBeforeUnmount(cancel)
</script>
<template><ReviewSignalDetailDrawer :summary="row" :detail="detail" :loading="loading" :error="error" @retry="reload" /></template>
