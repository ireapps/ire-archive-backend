<!--
  EmbeddingMap.svelte — Canvas-based D3 scatter plot with zoom/pan.

  Renders thousands of points efficiently using <canvas>.
  D3 handles the zoom transform; we redraw on every zoom event.
-->
<script lang="ts">
	import { onMount } from 'svelte';
	import * as d3 from 'd3';
	import { getCategoryColor } from '$lib/types';
	import type { EmbeddingPoint, TooltipState } from '$lib/types';

	interface Props {
		points: EmbeddingPoint[];
		allPoints: EmbeddingPoint[];
		onHover: (state: TooltipState) => void;
		onClick: (point: EmbeddingPoint | null) => void;
	}

	let { points, allPoints, onHover, onClick }: Props = $props();

	let container: HTMLDivElement;
	let canvas: HTMLCanvasElement;
	let width = $state(800);
	let height = $state(600);
	let currentTransform: d3.ZoomTransform = d3.zoomIdentity;

	// Scales: map normalized [-1, 1] coords to pixel space
	function xScale() {
		const pad = 40;
		return d3.scaleLinear().domain([-1.05, 1.05]).range([pad, width - pad]);
	}

	function yScale() {
		const pad = 40;
		return d3.scaleLinear().domain([-1.05, 1.05]).range([pad, height - pad]);
	}

	const BASE_RADIUS = 3;
	const HOVER_RADIUS = 7;

	// --- Hit detection using a quadtree for O(log n) lookups ---
	let quadtree: d3.Quadtree<EmbeddingPoint>;

	$effect(() => {
		// Rebuild quadtree whenever points change
		const sx = xScale();
		const sy = yScale();
		quadtree = d3
			.quadtree<EmbeddingPoint>()
			.x((d) => sx(d.x))
			.y((d) => sy(d.y))
			.addAll(points);
	});

	function findNearest(px: number, py: number, radius: number): EmbeddingPoint | undefined {
		if (!quadtree) return undefined;
		// Invert the zoom transform to get data-space pixel coords
		const [dx, dy] = currentTransform.invert([px, py]);
		return quadtree.find(dx, dy, radius / currentTransform.k);
	}

	// --- Rendering ---
	function draw() {
		if (!canvas) return;
		const ctx = canvas.getContext('2d');
		if (!ctx) return;

		const dpr = window.devicePixelRatio || 1;
		canvas.width = width * dpr;
		canvas.height = height * dpr;
		ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

		// Clear
		ctx.fillStyle = '#0a0a0f';
		ctx.fillRect(0, 0, width, height);

		const sx = xScale();
		const sy = yScale();
		const t = currentTransform;
		const r = Math.max(1.5, BASE_RADIUS * Math.min(t.k, 3));

		// Draw points
		for (const p of points) {
			const px = t.applyX(sx(p.x));
			const py = t.applyY(sy(p.y));

			// Skip off-screen points
			if (px < -r || px > width + r || py < -r || py > height + r) continue;

			ctx.beginPath();
			ctx.arc(px, py, r, 0, Math.PI * 2);
			ctx.fillStyle = getCategoryColor(p.category || 'unknown');
			ctx.globalAlpha = 0.75;
			ctx.fill();
		}

		ctx.globalAlpha = 1;
	}

	$effect(() => {
		// Redraw whenever points, width, or height change
		void points;
		void width;
		void height;
		draw();
	});

	onMount(() => {
		// Observe container size
		const ro = new ResizeObserver((entries) => {
			for (const entry of entries) {
				width = entry.contentRect.width;
				height = entry.contentRect.height;
			}
		});
		ro.observe(container);

		// D3 zoom behavior
		const zoomBehavior = d3
			.zoom<HTMLCanvasElement, unknown>()
			.scaleExtent([0.3, 30])
			.on('zoom', (event: d3.D3ZoomEvent<HTMLCanvasElement, unknown>) => {
				currentTransform = event.transform;
				draw();
			});

		d3.select(canvas).call(zoomBehavior);

		// Mouse move → tooltip via quadtree
		canvas.addEventListener('mousemove', (e) => {
			const rect = canvas.getBoundingClientRect();
			const mx = e.clientX - rect.left;
			const my = e.clientY - rect.top;

			const hit = findNearest(mx, my, 12);

			if (hit) {
				onHover({
					visible: true,
					x: e.clientX,
					y: e.clientY,
					point: hit
				});
				canvas.style.cursor = 'pointer';

				// Draw highlight ring
				draw();
				const ctx = canvas.getContext('2d');
				if (ctx) {
					const dpr = window.devicePixelRatio || 1;
					ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
					const sx2 = xScale();
					const sy2 = yScale();
					const px = currentTransform.applyX(sx2(hit.x));
					const py = currentTransform.applyY(sy2(hit.y));
					ctx.beginPath();
					ctx.arc(px, py, HOVER_RADIUS * Math.min(currentTransform.k, 3), 0, Math.PI * 2);
					ctx.strokeStyle = '#fff';
					ctx.lineWidth = 2;
					ctx.stroke();
					ctx.beginPath();
					ctx.arc(px, py, HOVER_RADIUS * Math.min(currentTransform.k, 3), 0, Math.PI * 2);
					ctx.fillStyle = getCategoryColor(hit.category || 'unknown');
					ctx.globalAlpha = 0.9;
					ctx.fill();
					ctx.globalAlpha = 1;
				}
			} else {
				onHover({ visible: false, x: 0, y: 0, point: null });
				canvas.style.cursor = 'grab';
			}
		});

		canvas.addEventListener('mouseleave', () => {
			onHover({ visible: false, x: 0, y: 0, point: null });
			canvas.style.cursor = 'grab';
			draw();
		});

		canvas.addEventListener('click', (e) => {
			const rect = canvas.getBoundingClientRect();
			const mx = e.clientX - rect.left;
			const my = e.clientY - rect.top;
			const hit = findNearest(mx, my, 12);
			onClick(hit ?? null);
		});

		return () => {
			ro.disconnect();
		};
	});
</script>

<div class="map-container" bind:this={container}>
	<canvas bind:this={canvas} style="width: {width}px; height: {height}px; cursor: grab;"></canvas>
	<div class="zoom-hint">Scroll to zoom · Drag to pan</div>
</div>

<style>
	.map-container {
		width: 100%;
		height: 100%;
		position: relative;
	}

	canvas {
		display: block;
	}

	.zoom-hint {
		position: absolute;
		bottom: 12px;
		right: 16px;
		font-size: 0.7rem;
		color: #555;
		pointer-events: none;
	}
</style>
