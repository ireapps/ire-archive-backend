<script lang="ts">
	import { onMount } from 'svelte';
	import EmbeddingMap from '$lib/components/EmbeddingMap.svelte';
	import Legend from '$lib/components/Legend.svelte';
	import Tooltip from '$lib/components/Tooltip.svelte';
	import PointDetail from '$lib/components/PointDetail.svelte';
	import type { EmbeddingPoint, EmbeddingResponse, TooltipState } from '$lib/types';

	const API_BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000';

	let points: EmbeddingPoint[] = $state([]);
	let meta: EmbeddingResponse['meta'] | null = $state(null);
	let loading = $state(true);
	let error: string | null = $state(null);

	// Interaction state
	let tooltip: TooltipState = $state({ visible: false, x: 0, y: 0, point: null });
	let selectedPoint: EmbeddingPoint | null = $state(null);
	let activeCategories: Set<string> = $state(new Set());
	let allCategories: string[] = $state([]);

	// Filtered points based on active categories
	let filteredPoints = $derived(
		activeCategories.size === 0
			? points
			: points.filter((p) => activeCategories.has(p.category || 'unknown'))
	);

	async function loadData() {
		loading = true;
		error = null;
		try {
			const res = await fetch(`${API_BASE}/visualize/embeddings`);
			if (!res.ok) throw new Error(`API error: ${res.status} ${res.statusText}`);
			const data: EmbeddingResponse = await res.json();
			points = data.points;
			meta = data.meta;
			// Extract unique categories sorted by count
			if (meta?.categories) {
				allCategories = Object.entries(meta.categories)
					.sort((a, b) => b[1] - a[1])
					.map(([cat]) => cat);
			}
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load embeddings';
		} finally {
			loading = false;
		}
	}

	function handleToggleCategory(category: string) {
		const next = new Set(activeCategories);
		if (next.has(category)) {
			next.delete(category);
		} else {
			next.add(category);
		}
		activeCategories = next;
	}

	function handleShowAll() {
		activeCategories = new Set();
	}

	function handlePointHover(detail: TooltipState) {
		tooltip = detail;
	}

	function handlePointClick(point: EmbeddingPoint | null) {
		selectedPoint = point;
	}

	onMount(loadData);
</script>

<svelte:head>
	<title>IRE Archive — Embedding Explorer</title>
	<meta name="description" content="Interactive visualization of IRE archive document embeddings" />
</svelte:head>

<div class="app">
	<header>
		<h1>IRE Archive Embedding Explorer</h1>
		{#if meta}
			<p class="meta">
				{meta.total.toLocaleString()} documents · UMAP projection · n_neighbors={meta.umap_params
					.n_neighbors} · min_dist={meta.umap_params.min_dist}
				{#if meta.elapsed_seconds}
					· computed in {meta.elapsed_seconds}s
				{/if}
			</p>
		{/if}
	</header>

	{#if loading}
		<div class="loading">
			<div class="spinner"></div>
			<p>Computing UMAP projection…</p>
			<p class="sub">This may take a moment for large collections.</p>
		</div>
	{:else if error}
		<div class="error">
			<p>⚠ {error}</p>
			<button onclick={loadData}>Retry</button>
		</div>
	{:else}
		<div class="layout">
			<aside class="sidebar">
				<Legend
					categories={allCategories}
					counts={meta?.categories ?? {}}
					{activeCategories}
					onToggle={handleToggleCategory}
					onShowAll={handleShowAll}
				/>

				{#if selectedPoint}
					<PointDetail point={selectedPoint} onClose={() => handlePointClick(null)} />
				{/if}
			</aside>

			<main class="canvas-area">
				<EmbeddingMap
					points={filteredPoints}
					allPoints={points}
					onHover={handlePointHover}
					onClick={handlePointClick}
				/>
			</main>
		</div>

		<Tooltip {...tooltip} />
	{/if}
</div>

<style>
	:global(body) {
		margin: 0;
		font-family:
			'Inter',
			-apple-system,
			BlinkMacSystemFont,
			'Segoe UI',
			sans-serif;
		background: #0a0a0f;
		color: #e0e0e8;
		overflow: hidden;
		height: 100vh;
	}

	.app {
		display: flex;
		flex-direction: column;
		height: 100vh;
	}

	header {
		padding: 0.75rem 1.25rem;
		border-bottom: 1px solid #1e1e2e;
		flex-shrink: 0;
	}

	header h1 {
		margin: 0;
		font-size: 1.1rem;
		font-weight: 600;
		color: #f0f0f8;
	}

	header .meta {
		margin: 0.25rem 0 0;
		font-size: 0.75rem;
		color: #888;
	}

	.layout {
		display: flex;
		flex: 1;
		overflow: hidden;
	}

	.sidebar {
		width: 260px;
		flex-shrink: 0;
		padding: 1rem;
		border-right: 1px solid #1e1e2e;
		overflow-y: auto;
		display: flex;
		flex-direction: column;
		gap: 1rem;
	}

	.canvas-area {
		flex: 1;
		position: relative;
		overflow: hidden;
	}

	.loading {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		flex: 1;
		gap: 0.75rem;
	}

	.loading p {
		color: #aaa;
		font-size: 0.95rem;
	}

	.loading .sub {
		font-size: 0.8rem;
		color: #666;
	}

	.spinner {
		width: 32px;
		height: 32px;
		border: 3px solid #2a2a3a;
		border-top-color: #6c8cff;
		border-radius: 50%;
		animation: spin 0.8s linear infinite;
	}

	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}

	.error {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		flex: 1;
		gap: 0.75rem;
	}

	.error p {
		color: #ff6b6b;
	}

	.error button {
		background: #2a2a3a;
		color: #e0e0e8;
		border: 1px solid #3a3a4a;
		padding: 0.5rem 1rem;
		border-radius: 4px;
		cursor: pointer;
	}

	.error button:hover {
		background: #3a3a4a;
	}
</style>
