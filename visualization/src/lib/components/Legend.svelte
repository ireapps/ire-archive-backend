<!--
  Legend.svelte — Category filter legend with color swatches and counts.
-->
<script lang="ts">
	import { getCategoryColor } from '$lib/types';

	interface Props {
		categories: string[];
		counts: Record<string, number>;
		activeCategories: Set<string>;
		onToggle: (category: string) => void;
		onShowAll: () => void;
	}

	let { categories, counts, activeCategories, onToggle, onShowAll }: Props = $props();

	function isActive(cat: string): boolean {
		return activeCategories.size === 0 || activeCategories.has(cat);
	}
</script>

<div class="legend">
	<div class="legend-header">
		<h3>Categories</h3>
		{#if activeCategories.size > 0}
			<button class="show-all" onclick={onShowAll}>Show all</button>
		{/if}
	</div>

	<ul>
		{#each categories as cat}
			<li>
				<button
					class="cat-btn"
					class:dimmed={!isActive(cat)}
					onclick={() => onToggle(cat)}
				>
					<span
						class="swatch"
						style="background: {getCategoryColor(cat)}; opacity: {isActive(cat) ? 1 : 0.3}"
					></span>
					<span class="label">{cat}</span>
					<span class="count">{(counts[cat] ?? 0).toLocaleString()}</span>
				</button>
			</li>
		{/each}
	</ul>
</div>

<style>
	.legend {
		display: flex;
		flex-direction: column;
	}

	.legend-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-bottom: 0.5rem;
	}

	h3 {
		margin: 0;
		font-size: 0.8rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: #888;
	}

	.show-all {
		background: none;
		border: none;
		color: #6c8cff;
		font-size: 0.7rem;
		cursor: pointer;
		padding: 0;
	}

	.show-all:hover {
		text-decoration: underline;
	}

	ul {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.cat-btn {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		width: 100%;
		background: none;
		border: none;
		padding: 0.35rem 0.5rem;
		border-radius: 4px;
		cursor: pointer;
		color: #e0e0e8;
		font-size: 0.8rem;
		transition: background 0.15s;
	}

	.cat-btn:hover {
		background: #1a1a2a;
	}

	.cat-btn.dimmed {
		opacity: 0.45;
	}

	.swatch {
		width: 10px;
		height: 10px;
		border-radius: 2px;
		flex-shrink: 0;
		transition: opacity 0.2s;
	}

	.label {
		flex: 1;
		text-align: left;
	}

	.count {
		color: #666;
		font-size: 0.7rem;
		font-variant-numeric: tabular-nums;
	}
</style>
