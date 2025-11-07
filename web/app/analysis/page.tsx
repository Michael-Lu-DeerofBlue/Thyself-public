export default function AnalysisPage() {
  return (
    <main className="px-6 py-10 max-w-5xl mx-auto flex flex-col gap-10">
      <header className="flex items-center justify-between">
        <a href="/" className="h-title text-3xl font-bold">Thyself</a>
      </header>
      <div>
        <div className="border rounded p-4">
          {/* Client-side orchestrator */}
          <ClientSlot />
        </div>
      </div>
    </main>
  );
}

// Small wrapper to import client-only component without SSR warnings
function ClientSlot() {
  const Comp = require('@/components/AnalysisClient').default;
  return <Comp />;
}

