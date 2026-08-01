export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: number;
}

export interface Conversation {
  id: string;
  title: string;
  messages: ChatMessage[];
  createdAt: number;
  updatedAt: number;
}

const STORAGE_KEY = "cyber-ai-conversations";
const MAX_CONVERSATIONS = 30;

// Helper to generate a unique ID safely
function generateId(): string {
  if (typeof window !== "undefined" && window.crypto && window.crypto.randomUUID) {
    try {
      return window.crypto.randomUUID();
    } catch (_) {}
  }
  return Math.random().toString(36).substring(2, 15) + Date.now().toString(36);
}

// Safe check for localStorage availability
function isStorageAvailable(): boolean {
  if (typeof window === "undefined") return false;
  try {
    const testKey = "__storage_test__";
    window.localStorage.setItem(testKey, testKey);
    window.localStorage.removeItem(testKey);
    return true;
  } catch (e) {
    return false;
  }
}

export function getConversations(): Conversation[] {
  if (!isStorageAvailable()) return [];
  try {
    const data = window.localStorage.getItem(STORAGE_KEY);
    if (!data) return [];
    const parsed = JSON.parse(data) as Conversation[];
    if (!Array.isArray(parsed)) return [];
    return parsed.sort((a, b) => b.updatedAt - a.updatedAt);
  } catch (error) {
    console.error("Error reading conversations from localStorage:", error);
    return [];
  }
}

export function getConversation(id: string): Conversation | null {
  const list = getConversations();
  return list.find((c) => c.id === id) || null;
}

export function saveConversation(conversation: Conversation): void {
  if (!isStorageAvailable()) return;
  try {
    const list = getConversations();
    const index = list.findIndex((c) => c.id === conversation.id);
    
    const updatedConversation = {
      ...conversation,
      updatedAt: Date.now(),
    };

    if (index !== -1) {
      list[index] = updatedConversation;
    } else {
      list.unshift(updatedConversation);
    }

    // Trim to max allowed conversations to prevent storage overflow
    const trimmedList = list.slice(0, MAX_CONVERSATIONS);
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(trimmedList));
  } catch (error) {
    console.error("Error saving conversation to localStorage:", error);
  }
}

export function deleteConversation(id: string): void {
  if (!isStorageAvailable()) return;
  try {
    const list = getConversations();
    const filtered = list.filter((c) => c.id !== id);
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(filtered));
  } catch (error) {
    console.error("Error deleting conversation from localStorage:", error);
  }
}

export function createNewConversation(): Conversation {
  const now = Date.now();
  return {
    id: generateId(),
    title: "Yeni Sohbet",
    messages: [],
    createdAt: now,
    updatedAt: now,
  };
}

export function generateTitleFromFirstMessage(content: string): string {
  const cleanContent = content.trim().replace(/\s+/g, " ");
  if (cleanContent.length <= 30) {
    return cleanContent;
  }
  return cleanContent.substring(0, 28) + "...";
}