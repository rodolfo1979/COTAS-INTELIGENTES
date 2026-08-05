<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration {
    public function up(): void
    {
        Schema::create('customers', function (Blueprint $table) {
            $table->id();
            $table->string('name')->unique();
            $table->timestamps();
        });

        Schema::create('parts', function (Blueprint $table) {
            $table->id();
            $table->foreignId('customer_id')->constrained()->cascadeOnDelete();
            $table->string('part_number');
            $table->string('description')->nullable();
            $table->timestamps();
            $table->unique(['customer_id', 'part_number']);
        });

        Schema::create('drawings', function (Blueprint $table) {
            $table->id();
            $table->foreignId('part_id')->constrained()->cascadeOnDelete();
            $table->string('drawing_number');
            $table->timestamps();
            $table->unique(['part_id', 'drawing_number']);
        });

        Schema::create('drawing_revisions', function (Blueprint $table) {
            $table->id();
            $table->foreignId('drawing_id')->constrained()->cascadeOnDelete();
            $table->string('revision')->default('');
            $table->string('source_hash', 64);
            $table->string('original_pdf_path');
            $table->string('numbered_pdf_path')->nullable();
            $table->string('candidates_json_path')->nullable();
            $table->unsignedInteger('candidate_count')->default(0);
            $table->string('status')->default('pending');
            $table->foreignId('created_by')->nullable()->constrained('users')->nullOnDelete();
            $table->timestamp('approved_at')->nullable();
            $table->timestamps();
            $table->index(['revision', 'source_hash']);
        });

        Schema::create('dimension_marks', function (Blueprint $table) {
            $table->id();
            $table->foreignId('drawing_revision_id')->constrained()->cascadeOnDelete();
            $table->unsignedInteger('number');
            $table->unsignedInteger('page');
            $table->string('text')->nullable();
            $table->decimal('x', 10, 3);
            $table->decimal('y', 10, 3);
            $table->decimal('width', 10, 3)->default(0);
            $table->decimal('height', 10, 3)->default(0);
            $table->decimal('confidence', 5, 3)->default(0);
            $table->string('status')->default('proposed');
            $table->timestamps();
            $table->unique(['drawing_revision_id', 'number']);
        });
    }

    public function down(): void
    {
        Schema::dropIfExists('dimension_marks');
        Schema::dropIfExists('drawing_revisions');
        Schema::dropIfExists('drawings');
        Schema::dropIfExists('parts');
        Schema::dropIfExists('customers');
    }
};
