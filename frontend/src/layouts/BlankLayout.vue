<script setup>
import TechBackdrop from './components/TechBackdrop.vue'
</script>

<template>
  <div class="blank-layout">
    <TechBackdrop />
    <div class="blank-content">
      <router-view />
    </div>
  </div>
</template>

<style scoped>
.blank-layout {
  height: 100%;
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
}
.blank-content {
  position: relative;
  z-index: 1;
  width: 100%;
}
</style>
