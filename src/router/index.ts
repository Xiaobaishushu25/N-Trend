import { createRouter, createWebHashHistory } from 'vue-router'
import DashboardView from '../views/DashboardView.vue'
import ChartView from '../views/ChartView.vue'

export default createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', name: 'dashboard', component: DashboardView },
    {
      path: '/chart/:symbol',
      name: 'chart',
      component: ChartView,
      meta: { bare: true },
    },
  ],
})
