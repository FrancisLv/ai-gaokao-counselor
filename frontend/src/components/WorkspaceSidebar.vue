<script setup lang="ts">
import { computed } from "vue";

type ActivePage = "chat" | "wishlists" | "account";

const props = defineProps<{
  active: ActivePage;
  noteTitle?: string;
  noteText?: string;
  footText: string;
}>();

const navItems = computed(() => {
  const items = [
    {
      id: "chat",
      label: "对话规划",
      description:
        props.active === "chat" ? "新对话与最近记录" : "继续当前志愿规划",
      href: props.active === "chat" ? undefined : "/",
    },
    {
      id: "wishlists",
      label: "我的志愿表",
      description: "查看已保存的方案版本",
      href: props.active === "wishlists" ? undefined : "/wishlists.html",
    },
    {
      id: "account",
      label: "账户与安全",
      description: "管理登录与个人数据",
      href: props.active === "account" ? undefined : "/account.html",
    },
  ] as const;

  return props.active === "chat"
    ? items.filter((item) => item.id === "chat")
    : items;
});
</script>

<template>
  <aside class="workspace-sidebar">
    <a class="workspace-brand" href="/">
      <span class="brand-mark">愿</span>
      <span>
        <strong>知愿 · 志愿规划</strong>
        <small>对话式高考志愿助手</small>
      </span>
    </a>

    <nav class="workspace-nav" aria-label="页面导航">
      <a
        v-for="item in navItems"
        :key="item.id"
        :href="item.href"
        :class="{ active: item.id === active }"
        :aria-current="item.id === active ? 'page' : undefined"
      >
        <strong>{{ item.label }}</strong>
        <small>{{ item.description }}</small>
      </a>
    </nav>

    <div v-if="$slots.primary" class="workspace-primary">
      <slot name="primary" />
    </div>

    <div v-if="$slots.history" class="workspace-history">
      <slot name="history" />
    </div>

    <div class="workspace-footer">
      <section v-if="noteTitle && noteText" class="workspace-note">
        <strong>{{ noteTitle }}</strong>
        <p>{{ noteText }}</p>
      </section>
      <p class="workspace-foot">{{ footText }}</p>
    </div>
  </aside>
</template>
