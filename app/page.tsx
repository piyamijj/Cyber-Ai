"use client";

import React, { useState, useEffect, useRef } from "react";
import {
  Send,
  Plus,
  Trash2,
  Menu,
  X,
  Cpu,
  Loader2,
  Terminal,
  Sparkles,
  MessageSquare,
} from "lucide-react";
import {
  ChatMessage,
  Conversation,
  getConversations,
  saveConversation,
  deleteConversation,
  createNewConversation,
  generateTitleFromFirstMessage,
} from "@/lib/storage";

export default function CyberChat() {
  // State
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeConv, setActiveConv] = useState<Conversation | null>(null);
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [streamingContent, setStreamingContent] = useState("");

  // Refs
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Load conversations on mount
  useEffect(() => {
    const list = getConversations();
    setConversations(list);
    if (list.length > 0) {
      setActiveConv(list[0]);
    } else {
      // Start with a fresh in-memory conversation (not saved to localStorage yet)
      setActiveConv(createNewConversation());
    }
  }, []);

  // Auto-scroll to bottom when messages or streaming content changes
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [activeConv?.messages, streamingContent]);

  // Auto-grow textarea height
  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      textareaRef.current.style.height = `${Math.min(
        textareaRef.current.scrollHeight,
        200
      )}px`;
    }
  }, [input]);

  // Create a new chat session
  const handleNewChat = () => {
    const newConv = createNewConversation();
    setActiveConv(newConv);
    setSidebarOpen(false);
    setInput("");
    setStreamingContent("");
  };

  // Select an existing conversation
  const handleSelectConversation = (conv: Conversation) => {
    setActiveConv(conv);
    setSidebarOpen(false);
    setInput("");
    setStreamingContent("");
  };

  // Delete a conversation
  const handleDeleteConversation = (e: React.MouseEvent, id: string) => {
    e.stopPropagation();
    if (window.confirm("Bu sohbeti silmek istediğinizden emin misiniz?")) {
      deleteConversation(id);
      const updatedList = getConversations();
      setConversations(updatedList);

      // If we deleted the active conversation, switch to another one or create a new one
      if (activeConv?.id === id) {
        if (updatedList.length > 0) {
          setActiveConv(updatedList[0]);
        } else {
          setActiveConv(createNewConversation());
        }
      }
    }
  };

  // Send message handler
  const handleSend = async (textToSend?: string) => {
    const messageText = textToSend !== undefined ? textToSend : input;
    if (!messageText.trim() || isStreaming || !activeConv) return;

    if (textToSend === undefined) {
      setInput("");
    }

    const userMsgId = Math.random().toString(36).substring(7);
    const userMessage: ChatMessage = {
      id: userMsgId,
      role: "user",
      content: messageText.trim(),
      timestamp: Date.now(),
    };

    // 1. Update active conversation state with user message
    let updatedMessages = [...activeConv.messages, userMessage];
    let updatedConv: Conversation = {
      ...activeConv,
      messages: updatedMessages,
      updatedAt: Date.now(),
    };

    // If this is the first message, generate a title
    if (activeConv.messages.length === 0) {
      updatedConv.title = generateTitleFromFirstMessage(userMessage.content);
    }

    setActiveConv(updatedConv);
    saveConversation(updatedConv);

    // Refresh sidebar list
    setConversations(getConversations());

    // 2. Prepare for streaming
    setIsStreaming(true);
    setStreamingContent("");

    try {
      // Map messages to the format expected by the API proxy
      const apiMessages = updatedMessages.map((m) => ({
        role: m.role,
        content: m.content,
      }));

      const response = await fetch("/api/chat", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ messages: apiMessages }),
      });

      if (!response.ok) {
        let errorData;
        try {
          errorData = await response.json();
        } catch (_) {}
        throw new Error(
          errorData?.message || `Sunucu hatası: ${response.statusText}`
        );
      }

      if (!response.body) {
        throw new Error("Yanıt gövdesi boş.");
      }

      // 3. Read stream
      const reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let accumulatedText = "";

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;

        const chunk = decoder.decode(value, { stream: true });
        const lines = chunk.split("\n");

        for (const line of lines) {
          const cleanLine = line.trim();
          if (!cleanLine) continue;

          if (cleanLine.startsWith("data: ")) {
            const dataStr = cleanLine.slice(6);
            if (dataStr === "[DONE]") continue;

            try {
              const parsed = JSON.parse(dataStr);
              const content = parsed.choices?.[0]?.delta?.content || "";
              if (content) {
                accumulatedText += content;
                setStreamingContent(accumulatedText);
              }
            } catch (e) {
              // Ignore parsing errors for incomplete chunks
            }
          }
        }
      }

      // 4. Stream finished successfully, save assistant message
      const assistantMsgId = Math.random().toString(36).substring(7);
      const assistantMessage: ChatMessage = {
        id: assistantMsgId,
        role: "assistant",
        content: accumulatedText,
        timestamp: Date.now(),
      };

      const finalConv: Conversation = {
        ...updatedConv,
        messages: [...updatedConv.messages, assistantMessage],
        updatedAt: Date.now(),
      };

      setActiveConv(finalConv);
      saveConversation(finalConv);
      setConversations(getConversations());

    } catch (error: any) {
      console.error("Chat error:", error);
      
      // Save error message as assistant response so user knows what went wrong
      const errorMsgId = Math.random().toString(36).substring(7);
      const errorMessage: ChatMessage = {
        id: errorMsgId,
        role: "assistant",
        content: `⚠️ Hata: ${error.message || "Sunucuyla bağlantı kurulamadı. Lütfen Oracle sunucunuzun açık ve erişilebilir olduğundan emin olun."}`,
        timestamp: Date.now(),
      };

      const finalConvWithError: Conversation = {
        ...updatedConv,
        messages: [...updatedConv.messages, errorMessage],
        updatedAt: Date.now(),
      };

      setActiveConv(finalConvWithError);
      saveConversation(finalConvWithError);
      setConversations(getConversations());
    } finally {
      setIsStreaming(false);
      setStreamingContent("");
    }
  };

  // Handle keypress in textarea
  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  // Suggestion chips click handler
  const handleSuggestionClick = (prompt: string) => {
    handleSend(prompt);
  };

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-cyber-bg text-cyber-text grid-bg relative">
      
      {/* Mobile Sidebar Backdrop */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/60 backdrop-blur-sm md:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      {/* Sidebar Panel */}
      <aside
        className={`fixed inset-y-0 left-0 z-50 flex w-[280px] flex-col border-r border-cyber-border bg-cyber-surface transition-transform duration-300 ease-in-out md:static md:translate-x-0 ${
          sidebarOpen ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        {/* Sidebar Header */}
        <div className="flex h-16 items-center justify-between px-4 border-b border-cyber-border">
          <div className="flex items-center gap-2">
            <div className="relative flex h-8 w-8 items-center justify-center rounded-lg border border-cyber-accent/30 bg-cyber-panel glow-active">
              <Cpu className="h-4 w-4 text-cyber-accent" />
            </div>
            <span className="text-lg font-bold tracking-wider text-white">
              CYBER<span className="text-cyber-accent">.AI</span>
            </span>
          </div>
          <button
            onClick={() => setSidebarOpen(false)}
            className="rounded-lg p-1 text-cyber-muted hover:bg-cyber-panel hover:text-white md:hidden"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* New Chat Button */}
        <div className="p-4">
          <button
            onClick={handleNewChat}
            className="flex w-full items-center justify-center gap-2 rounded-lg border border-cyber-accent/30 bg-cyber-panel/50 px-4 py-2.5 text-sm font-medium text-cyber-accent transition-all hover:bg-cyber-accent/10 hover:border-cyber-accent"
          >
            <Plus className="h-4 w-4" />
            Yeni Sohbet
          </button>
        </div>

        {/* Conversations List */}
        <div className="flex-1 overflow-y-auto px-3 pb-4 space-y-1">
          <div className="px-3 py-2 text-xs font-semibold uppercase tracking-wider text-cyber-muted">
            Geçmiş Konuşmalar
          </div>
          {conversations.length === 0 ? (
            <div className="px-3 py-4 text-xs text-cyber-muted italic">
              Henüz kayıtlı sohbet yok.
            </div>
          ) : (
            conversations.map((conv) => {
              const isActive = activeConv?.id === conv.id;
              return (
                <div
                  key={conv.id}
                  onClick={() => handleSelectConversation(conv)}
                  className={`group flex items-center justify-between rounded-lg px-3 py-2.5 text-sm cursor-pointer transition-all ${
                    isActive
                      ? "bg-cyber-panel border border-cyber-border text-white"
                      : "text-cyber-muted hover:bg-cyber-panel/30 hover:text-cyber-text"
                  }`}
                >
                  <div className="flex items-center gap-2.5 min-w-0 flex-1">
                    <MessageSquare className={`h-4 w-4 shrink-0 ${isActive ? "text-cyber-accent" : "text-cyber-muted"}`} />
                    <span className="truncate font-medium">{conv.title}</span>
                  </div>
                  <button
                    onClick={(e) => handleDeleteConversation(e, conv.id)}
                    className="opacity-0 group-hover:opacity-100 p-1 rounded hover:bg-cyber-surface text-cyber-muted hover:text-red-400 transition-all"
                    title="Sohbeti Sil"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              );
            })
          )}
        </div>

        {/* Sidebar Footer */}
        <div className="p-4 border-t border-cyber-border bg-cyber-bg/50">
          <div className="flex items-center gap-2 text-xs text-cyber-muted">
            <Terminal className="h-3.5 w-3.5 text-cyber-accent" />
            <span>Qwen2.5-14B · Oracle Cloud</span>
          </div>
        </div>
      </aside>

      {/* Main Chat Area */}
      <main className="flex flex-1 flex-col h-full min-w-0 relative">
        
        {/* Top Header */}
        <header className="flex h-16 items-center justify-between px-4 border-b border-cyber-border bg-cyber-surface/80 backdrop-blur-md z-10">
          <div className="flex items-center gap-3">
            <button
              onClick={() => setSidebarOpen(true)}
              className="rounded-lg p-1.5 text-cyber-muted hover:bg-cyber-panel hover:text-white md:hidden"
            >
              <Menu className="h-5 w-5" />
            </button>
            <div className="min-w-0">
              <h1 className="text-sm font-semibold text-white truncate">
                {activeConv && activeConv.messages.length > 0
                  ? activeConv.title
                  : "Cyber AI"}
              </h1>
              <p className="text-[10px] text-cyber-muted flex items-center gap-1">
                <span className="h-1.5 w-1.5 rounded-full bg-emerald-500 animate-pulse" />
                Oracle Sunucusu Aktif
              </p>
            </div>
          </div>
        </header>

        {/* Messages Container */}
        <div className="flex-1 overflow-y-auto p-4 md:p-6 space-y-6">
          {activeConv && activeConv.messages.length === 0 && !isStreaming ? (
            /* Empty State / Welcome Screen */
            <div className="flex h-full flex-col items-center justify-center max-w-xl mx-auto text-center space-y-8">
              <div className="relative flex h-16 w-16 items-center justify-center rounded-2xl border border-cyber-accent/30 bg-cyber-surface glow-active">
                <Cpu className="h-8 w-8 text-cyber-accent" />
              </div>
              <div className="space-y-2">
                <h2 className="text-2xl font-bold tracking-tight text-white">
                  Siber Dünyaya Hoş Geldiniz
                </h2>
                <p className="text-sm text-cyber-muted">
                  Oracle Cloud üzerinde barındırılan Qwen2.5-14B modeli ile çalışan, sade ve karanlık temalı sohbet arayüzü.
                </p>
              </div>

              {/* Suggestion Chips */}
              <div className="w-full space-y-3 pt-4">
                <div className="text-xs font-semibold uppercase tracking-wider text-cyber-muted flex items-center justify-center gap-1.5">
                  <Sparkles className="h-3.5 w-3.5 text-cyber-accent" />
                  Önerilen Başlangıçlar
                </div>
                <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                  {[
                    "Bana kısa bir bilim kurgu hikayesi yaz.",
                    "Python ile basit bir web scraping scripti nasıl yazılır?",
                    "Kuantum bilgisayarların çalışma mantığını sadeleştirerek anlat.",
                    "Bugün odaklanmamı artıracak 3 pratik teknik öner.",
                  ].map((prompt, idx) => (
                    <button
                      key={idx}
                      onClick={() => handleSuggestionClick(prompt)}
                      className="text-left text-xs p-3 rounded-lg border border-cyber-border bg-cyber-surface/50 hover:bg-cyber-panel hover:border-cyber-accent/30 text-cyber-text transition-all duration-200"
                    >
                      {prompt}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          ) : (
            /* Message List */
            <div className="max-w-3xl mx-auto space-y-6">
              {activeConv?.messages.map((msg) => {
                const isUser = msg.role === "user";
                return (
                  <div
                    key={msg.id}
                    className={`flex ${isUser ? "justify-end" : "justify-start"}`}
                  >
                    <div
                      className={`max-w-[85%] rounded-xl px-4 py-3 text-sm leading-relaxed whitespace-pre-wrap border ${
                        isUser
                          ? "bg-cyber-panel border-cyber-border text-white"
                          : "bg-cyber-surface border-cyber-border/50 text-cyber-text"
                      }`}
                    >
                      {msg.content}
                    </div>
                  </div>
                );
              })}

              {/* Streaming Message Placeholder */}
              {isStreaming && streamingContent && (
                <div className="flex justify-start">
                  <div className="max-w-[85%] rounded-xl px-4 py-3 text-sm leading-relaxed whitespace-pre-wrap border bg-cyber-surface border-cyber-border/50 text-cyber-text">
                    {streamingContent}
                    <span className="inline-block w-1.5 h-4 ml-1 bg-cyber-accent animate-pulse align-middle" />
                  </div>
                </div>
              )}

              {/* Loading Indicator (before first token arrives) */}
              {isStreaming && !streamingContent && (
                <div className="flex justify-start">
                  <div className="flex items-center gap-2 rounded-xl px-4 py-3 border bg-cyber-surface border-cyber-border/50 text-cyber-muted text-sm">
                    <Loader2 className="h-4 w-4 animate-spin text-cyber-accent" />
                    <span>Düşünülüyor...</span>
                  </div>
                </div>
              )}

              <div ref={messagesEndRef} />
            </div>
          )}
        </div>

        {/* Bottom Input Bar */}
        <div className="p-4 border-t border-cyber-border bg-cyber-surface/40 backdrop-blur-md">
          <div className="max-w-3xl mx-auto">
            <form
              onSubmit={(e) => {
                e.preventDefault();
                handleSend();
              }}
              className="relative flex items-end gap-2 rounded-xl border border-cyber-border bg-cyber-panel px-3 py-2.5 focus-within:border-cyber-accent/50 focus-within:ring-1 focus-within:ring-cyber-accent/20 transition-all"
            >
              <textarea
                ref={textareaRef}
                rows={1}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder="Mesajınızı yazın..."
                disabled={isStreaming}
                className="flex-1 resize-none bg-transparent text-sm text-white placeholder-cyber-muted focus:outline-none max-h-[200px] min-h-[20px] py-1"
              />
              <button
                type="submit"
                disabled={isStreaming || !input.trim()}
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-cyber-accent text-black hover:bg-cyber-accentDim disabled:bg-cyber-panel disabled:text-cyber-muted disabled:border disabled:border-cyber-border transition-all"
              >
                <Send className="h-4 w-4" />
              </button>
            </form>
            <div className="mt-2 text-center text-[10px] text-cyber-muted">
              Cyber AI, Oracle Cloud sunucunuzdaki Qwen2.5-14B modelini kullanır.
            </div>
          </div>
        </div>

      </main>
    </div>
  );
}