<script setup>
import AppSidebar from './components/AppSidebar.vue'
import AppTopbar from './components/AppTopbar.vue'
import TechBackdrop from './components/TechBackdrop.vue'
</script>

<template>
  <div class="main-layout">
    <TechBackdrop />
    <AppSidebar />
    <div class="body">
      <AppTopbar />
      <main class="content">
        <router-view v-slot="{ Component }">
          <transition name="fade" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </main>
    </div>
  </div>
</template>

<style scoped>
.main-layout {
  display: flex;
  height: 100%;
  position: relative;
}
.body {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  position: relative;
  z-index: 1;
}
.content {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
}
</style>
