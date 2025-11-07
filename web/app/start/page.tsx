import SectionCard from '@/components/SectionCard';

export default function StartPage() {
  const extLink = process.env.NEXT_PUBLIC_EXTENSION_LINK || '#';
  return (
    <main className="min-h-screen flex flex-col items-center justify-start gap-8 pt-24 px-6 text-center">
      <h1 className="h-title text-4xl font-bold">Start</h1>
      <div className="w-full max-w-2xl text-left">
        <SectionCard title="Welcome">
          <div className="space-y-5">
            <section>
              <h3 className="h-title text-lg font-semibold mb-1">What this is</h3>
              <p className="text-sm text-neutral-800">
                Thyself recommends one thoughtful piece a day and shows you an honest profile of your interests.
                No feeds. No infinite scrolling.
              </p>
            </section>

            <section>
              <h3 className="h-title text-lg font-semibold mb-1">Add the extension (Chrome/Edge)</h3>
              <ol className="list-decimal list-inside space-y-1 text-sm text-neutral-800">
                <li>
                  Install: {' '}
                  {extLink !== '#' ? (
                    <a href={extLink} target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline">Thyself Extension</a>
                  ) : (
                    <span className="text-neutral-700">&lt;LINK_TO_EXTENSION&gt;</span>
                  )}
                </li>
                <li>Pin the extension to the toolbar.</li>
              </ol>
            </section>

            <section>
              <h3 className="h-title text-lg font-semibold mb-1">How it works</h3>
              <ul className="list-disc list-inside space-y-1 text-sm text-neutral-800">
                <li>The extension reads titles from your social media feeds (only YouTube now).</li>
                <li>It builds a profile of you regarding your interest topics.</li>
                <li>Based on these points, we recommend a piece of media to consume for you (only New York Times article now).</li>
              </ul>
            </section>

            <section>
              <h3 className="h-title text-lg font-semibold mb-1">Steps</h3>
              <ol className="list-decimal list-inside space-y-1 text-sm text-neutral-800">
                <li>Go to YouTube and scroll a bit.</li>
                <li>
                  Open the {' '}
                  <a href="/analysis" className="text-blue-600 hover:underline">analysis page</a>
                  {' '} to build and view your profile.
                </li>
                <li>
                  Visit {' '}
                  <a href="/piece" className="text-blue-600 hover:underline">/piece</a>
                  {' '} to see today’s single recommendation.
                </li>
              </ol>
            </section>

            <section>
              <h3 className="h-title text-lg font-semibold mb-1">Privacy summary</h3>
              <ul className="list-disc list-inside space-y-1 text-sm text-neutral-800">
                <li>We only process captured titles, URLs, and timestamps.</li>
                <li>No keystrokes, no screenshots, no third-party trackers.</li>
                <li>You can delete your data anytime.</li>
              </ul>
            </section>
          </div>
        </SectionCard>
      </div>
    </main>
  );
}
