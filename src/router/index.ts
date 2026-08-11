import { createRouter, createWebHashHistory } from 'vue-router'
import DashboardView from '../views/DashboardView.vue'
import ChartView from '../views/ChartView.vue'
import NotificationsView from '../views/NotificationsView.vue'
import ReviewView from '../views/ReviewView.vue'
import SettingsView from '../views/SettingsView.vue'

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
    {
      path: '/settings',
      name: 'settings',
      component: SettingsView,
      meta: { bare: true },
    },
    {
      path: '/review',
      name: 'review',
      component: ReviewView,
      meta: { bare: true },
    },
    {
      path: '/notifications',
      name: 'notifications',
      component: NotificationsView,
      meta: { bare: true },
    },
  ],
})
