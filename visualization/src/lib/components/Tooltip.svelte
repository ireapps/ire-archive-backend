<!--
  Tooltip.svelte — Floating tooltip that follows the cursor on point hover.
-->
<script lang="ts">
	import { getCategoryColor } from '$lib/types';
	import type { EmbeddingPoint } from '$lib/types';

	interface Props {
		visible: boolean;
		x: number;
		y: number;
		point: EmbeddingPoint | null;
	}

	let { visible, x, y, point }: Props = $props();
</script>

{#if visible && point}
	<div
		class="tooltip"
		style="left: {x + 14}px; top: {y - 10}px;"
	>
		<div class="title">{point.title || 'Untitled'}</div>

		<div class="row">
			<span
				class="cat-badge"
				style="background: {getCategoryColor(point.category || 'unknown')}"
			>
				{point.category || 'unknown'}
			</span>
			{#if point.year}
				<span class="year">{point.year}</span>
			{/if}
		</div>

		{#if point.conference}
			<div class="detail">{point.conference}</div>
		{/if}

		{#if point.text_preview}
			<div class="preview">{point.text_preview}</div>
		{/if}
	</div>
{/if}

<style>
	.tooltip {
		position: fixed;
		z-index: 1000;
		background: #1a1a2a;
		border: 1px solid #2a2a3a;
		border-radius: 6px;
		padding: 0.6rem 0.8rem;
		max-width: 300px;
		pointer-events: none;
		box-shadow: 0 4px 20px rgba(0, 0, 0, 0.5);
	}

	.title {
		font-weight: 600;
		font-size: 0.85rem;
		color: #f0f0f8;
		margin-bottom: 0.35rem;
		line-height: 1.3;
	}

	.row {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		margin-bottom: 0.3rem;
	}

	.cat-badge {
		display: inline-block;
		font-size: 0.65rem;
		padding: 0.1rem 0.4rem;
		border-radius: 3px;
		color: #0a0a0f;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}

	.year {
		font-size: 0.75rem;
		color: #aaa;
	}

	.detail {
		font-size: 0.7rem;
		color: #999;
		margin-bottom: 0.2rem;
	}

	.preview {
		font-size: 0.7rem;
		color: #777;
		line-height: 1.4;
		overflow: hidden;
		display: -webkit-box;
		-webkit-line-clamp: 3;
		line-clamp: 3;
		-webkit-box-orient: vertical;
	}
</style>
