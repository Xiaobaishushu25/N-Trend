import { createRouter, createWebHashHistory } from 'vue-router'
import DashboardView from '../views/DashboardView.vue'

export default createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', name: 'dashboard', component: DashboardView },
    {
      path: '/chart/:symbol',
      name: 'chart',
      component: () => import('../views/ChartView.vue'),
      meta: { bare: true },
    },
    {
      path: '/settings',
      name: 'settings',
      component: () => import('../views/SettingsView.vue'),
      meta: { bare: true },
    },
    {
      path: '/review',
      name: 'review',
      component: () => import('../views/ReviewView.vue'),
      meta: { bare: true },
    },
    {
      path: '/trade-review',
      name: 'trade-review',
      component: () => import('../views/TradeReviewView.vue'),
      meta: { bare: true },
    },
    {
      path: '/live-trader',
      name: 'live-trader',
      component: () => import('../views/LiveTraderView.vue'),
      meta: { bare: true },
    },
    {
      path: '/notifications',
      name: 'notifications',
      component: () => import('../views/NotificationsView.vue'),
      meta: { bare: true },
    },
  ],
})
