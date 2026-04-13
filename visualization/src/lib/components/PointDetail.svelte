<!--
  PointDetail.svelte — Sidebar panel showing full details for a clicked point.
-->
<script lang="ts">
	import { getCategoryColor } from '$lib/types';
	import type { EmbeddingPoint } from '$lib/types';

	interface Props {
		point: EmbeddingPoint;
		onClose: () => void;
	}

	let { point, onClose }: Props = $props();
</script>

<div class="detail-panel">
	<div class="detail-header">
		<h3>Selected Document</h3>
		<button class="close-btn" onclick={onClose} aria-label="Close detail panel">✕</button>
	</div>

	<div class="detail-body">
		<h4>{point.title || 'Untitled'}</h4>

		<div class="badges">
			<span
				class="cat-badge"
				style="background: {getCategoryColor(point.category || 'unknown')}"
			>
				{point.category || 'unknown'}
			</span>
			{#if point.year}
				<span class="year-badge">{point.year}</span>
			{/if}
		</div>

		{#if point.conference}
			<div class="field">
				<span class="field-label">Conference</span>
				<span class="field-value">{point.conference}</span>
			</div>
		{/if}

		{#if point.resource_id}
			<div class="field">
				<span class="field-label">Resource ID</span>
				<span class="field-value mono">{point.resource_id}</span>
			</div>
		{/if}

		<div class="field">
			<span class="field-label">Vector ID</span>
			<span class="field-value mono">{point.vector_id}</span>
		</div>

		{#if point.text_preview}
			<div class="field">
				<span class="field-label">Preview</span>
				<p class="text-preview">{point.text_preview}</p>
			</div>
		{/if}

		<div class="field">
			<span class="field-label">UMAP coords</span>
			<span class="field-value mono">({point.x.toFixed(4)}, {point.y.toFixed(4)})</span>
		</div>
	</div>
</div>

<style>
	.detail-panel {
		border: 1px solid #2a2a3a;
		border-radius: 6px;
		background: #12121e;
		overflow: hidden;
	}

	.detail-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 0.5rem 0.75rem;
		border-bottom: 1px solid #1e1e2e;
	}

	.detail-header h3 {
		margin: 0;
		font-size: 0.75rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: #888;
	}

	.close-btn {
		background: none;
		border: none;
		color: #888;
		cursor: pointer;
		font-size: 0.9rem;
		padding: 0;
		line-height: 1;
	}

	.close-btn:hover {
		color: #ccc;
	}

	.detail-body {
		padding: 0.75rem;
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}

	h4 {
		margin: 0;
		font-size: 0.9rem;
		font-weight: 600;
		color: #f0f0f8;
		line-height: 1.3;
	}

	.badges {
		display: flex;
		align-items: center;
		gap: 0.4rem;
	}

	.cat-badge {
		display: inline-block;
		font-size: 0.6rem;
		padding: 0.1rem 0.4rem;
		border-radius: 3px;
		color: #0a0a0f;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}

	.year-badge {
		font-size: 0.7rem;
		color: #aaa;
		background: #1e1e2e;
		padding: 0.1rem 0.3rem;
		border-radius: 3px;
	}

	.field {
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
	}

	.field-label {
		font-size: 0.65rem;
		color: #666;
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}

	.field-value {
		font-size: 0.8rem;
		color: #ccc;
	}

	.mono {
		font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', monospace;
		font-size: 0.7rem;
		word-break: break-all;
	}

	.text-preview {
		margin: 0;
		font-size: 0.75rem;
		color: #999;
		line-height: 1.4;
	}
</style>
